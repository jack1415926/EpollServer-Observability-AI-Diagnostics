"""Paired evidence-policy development/holdout checks with synthetic read-only tools."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import shutil
from pathlib import Path
import time

import httpx

from .agent import SYSTEM, diagnose, failure_report
from .routing import ROUTER_SYSTEM
from .config import ROOT, load_environment, redact
from .evaluate import MeasuredProviders, ReplayTools, digest, read
from .retrieval import INDEX, Retriever
from .selection import REGISTRY
from .providers import ProviderError

DATA = ROOT/'evaluation/evidence_v1'


class AuditProvider(MeasuredProviders):
    def __init__(self, client):
        super().__init__(client)
        self.generations = []

    async def structured(self, messages, schema):
        value = await super().structured(messages, schema)
        self.generations.append({'kind':'route' if 'intent' in schema.get('properties',{}) else 'report',
                                 'text':redact(value)})
        return value

    async def chat(self, *args, **kwargs):
        value = await super().chat(*args, **kwargs)
        self.generations.append({'kind':'tool_decision', 'value':json.loads(redact(value))})
        return value


class FixedReplayTools(ReplayTools):
    def __init__(self, client, retriever, fixture, question):
        super().__init__(client, retriever, fixture)
        self.question, self.primed = question, False

    async def call(self, name, args):
        if not self.primed and name in ('get_server_health','get_worker_metrics','get_recent_events','read_server_logs'):
            self.primed = True
            for tool in ('get_server_health','get_worker_metrics','get_recent_events','query_experiments','read_server_logs'):
                await super().call(tool, {})
            self.read_contracts(list(read(REGISTRY)['topics']))
            await super().call('search_technical_docs', {'query':self.question[:500]})
        return await super().call(name,args)


async def run(args):
    load_environment()
    if args.output.exists(): raise ValueError('evidence_output_exists_use_new_directory')
    frozen=read(DATA/'frozen-inputs.json')
    if any(digest(DATA/name)!=value for name,value in frozen.items()):
        raise ValueError('evidence_dataset_changed')
    cases=[c for c in read(DATA/'cases.json') if
           (args.split=='all' or (c['split']=='holdout')==(args.split=='holdout')) and
           (not args.case or args.case==c['id'])]
    if not cases: raise ValueError('no_selected_evidence_cases')
    fixtures=read(DATA/'fixtures.json')
    retriever=Retriever(None,args.index)
    if any(digest(ROOT/c['path'])!=c['sha256'] for c in retriever.chunks):
        raise ValueError('evidence_index_stale')
    files={f'diagnosis/{name}' for name in ('agent.py','routing.py','providers.py','retrieval.py',
           'selection.py','evidence.py','observation_contracts.json','config.py','corpus.py','corpus_policy.json',
           'evaluate_evidence.py','evaluate.py','catalog.py')}
    files.update(c['path'] for c in retriever.chunks)
    files.update(ref['path'] for topic in read(REGISTRY)['topics'].values() for ref in topic['references'])
    files=sorted(files)
    manifest={'started_at':datetime.now(timezone.utc).isoformat(), 'dataset':frozen,
              'scope':'synthetic contract replay; project assistant authored; previously reviewed cases are development regressions, not a new blind holdout',
              'index_sha256':digest(args.index),'files':{name:digest(ROOT/name) for name in files},
              'split':args.split,'case':args.case,'strategy':args.strategy,'compare':args.compare,
              'comparison':'same implementation/prompt/guards; fixed eagerly reads all five fixture sources and all contracts, targeted follows route plus minimum contracts; independent model routing/generation calls'}
    args.output.mkdir(parents=True)
    (args.output/'prompts.json').write_text(json.dumps({'report':SYSTEM,'router':ROUTER_SYSTEM},ensure_ascii=False,indent=2)+'\n')
    for name in files:
        if Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('invalid_archive_source_path')
        destination=args.output/'sources'/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/name,destination)
    shutil.copy2(args.index,args.output/'index.npz')
    for name in ('cases.json','fixtures.json','labels.json','frozen-inputs.json'):
        (args.output/'data').mkdir(exist_ok=True)
        shutil.copy2(DATA/name,args.output/'data'/name)
    (args.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    rows=[]
    async with httpx.AsyncClient(trust_env=False) as client:
        for case in cases:
            strategies=('targeted','fixed') if args.compare and case['compare_fixed'] else (args.strategy,)
            for strategy in strategies:
                provider=AuditProvider(client)
                source=Retriever(provider,args.index)
                tools=(FixedReplayTools(client,source,fixtures[case['fixture']],case['question']) if strategy=='fixed'
                       else ReplayTools(client,source,fixtures[case['fixture']]))
                start=time.monotonic()
                row={'id':case['id'],'split':case['split'],'family':case['family'],'strategy':strategy}
                try:
                    async with asyncio.timeout(120):
                        report=await diagnose(case['question'],provider,tools)
                    row.update(status='ok', report=report, route_matches=report['kind']==case['expected_kind'],
                               semantic_review='pending_manual_review')
                except Exception as exc:
                    code=str(exc) if isinstance(exc,ProviderError) or str(exc) in (
                        'report_support_invalid','fabricated_evidence_reference') else type(exc).__name__
                    row.update(status='error',error=code,issues=getattr(exc,'issues',None),
                               fatal=isinstance(exc,(ProviderError,TimeoutError)),
                               report=failure_report(case['question'],code,provider,tools))
                row.update(seconds=round(time.monotonic()-start,4),calls=provider.calls,tools=tools.calls,
                           generations=provider.generations)
                rows.append(row)
                with (args.output/'results.jsonl').open('a') as output:
                    output.write(redact(row)+'\n')
                print(case['id'],strategy,row['status'],row.get('route_matches'),row.get('error'),flush=True)
                if row['status']=='error' and (not args.continue_on_error or row.get('fatal')):
                    raise RuntimeError('evidence_evaluation_failed_see_results')
    summary={'attempts':len(rows),'ok':sum(r['status']=='ok' for r in rows),'errors':sum(r['status']!='ok' for r in rows),
             'route_matches':sum(r.get('route_matches',False) for r in rows),'semantic_review':'pending_manual_review',
             'actual_api_calls':sum(len(r['calls']) for r in rows)}
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(summary,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index',type=Path,default=INDEX)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--split',choices=('dev','holdout','all'),default='dev')
    parser.add_argument('--case')
    parser.add_argument('--strategy',choices=('targeted','fixed'),default='targeted')
    parser.add_argument('--compare',action='store_true')
    parser.add_argument('--continue-on-error',action='store_true',help='Preserve failed attempts and proceed once to the next case; never retries')
    asyncio.run(run(parser.parse_args()))
