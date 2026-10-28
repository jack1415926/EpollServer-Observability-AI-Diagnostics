"""Small routing acceptance set; remote classification and fixed-tool replay, no live services."""
import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx

from .agent import diagnose
from .config import ROOT, load_environment
from .evaluate import AblationRetriever, DATA, MeasuredProviders, ReplayTools, digest, read
from .retrieval import INDEX
from .routing import route_question

CASES=ROOT/'evaluation/routing_v1/cases.json'


async def run(output, cases=CASES):
    load_environment()
    if output.exists():
        raise ValueError('routing_output_exists_use_new_directory')
    output.mkdir(parents=True)
    paths=['diagnosis/routing.py','diagnosis/agent.py','diagnosis/providers.py','diagnosis/evaluate_routing.py']
    (output/'manifest.json').write_text(json.dumps({'cases_sha256':digest(cases),'index_sha256':digest(INDEX),
        'files':{p:digest(ROOT/p) for p in paths},'model':os.getenv('DEEPSEEK_MODEL','deepseek-flash'),
        'scope':'routing acceptance; synthetic fixed-tool replay, not fault diagnosis accuracy'},ensure_ascii=False,indent=2)+'\n')
    rows=[]
    async with httpx.AsyncClient(trust_env=False) as client:
        for case in read(cases):
            provider=MeasuredProviders(client)
            row={'id':case['id'],'expected_intent':case['expected_intent'],'full_replay':case['full_replay']}
            try:
                async with asyncio.timeout(120):
                    if case['full_replay']:
                        tools=ReplayTools(client,AblationRetriever(provider),read(DATA/'fixtures.json')[case['fixture']])
                        report=await diagnose(case['question'],provider,tools)
                        row.update(report=report,tools=tools.calls,actual_intent=report['kind'])
                    else:
                        route,status=await route_question(case['question'],provider)
                        row.update(routing=route.model_dump(),route_status=status,actual_intent=route.intent)
                row.update(status='ok',intent_matches=row['actual_intent']==case['expected_intent'])
            except Exception as exc:
                row.update(status='error',error=type(exc).__name__+':'+str(exc),intent_matches=False)
            row['calls']=provider.calls
            rows.append(row)
            with (output/'results.jsonl').open('a') as stream:
                stream.write(json.dumps(row,ensure_ascii=False)+'\n')
            print(row['id'],row.get('actual_intent'),row['intent_matches'],flush=True)
    summary={'cases':len(rows),'intent_matches':sum(r['intent_matches'] for r in rows),
             'errors':sum(r['status']!='ok' for r in rows),'full_replays':sum(r['full_replay'] for r in rows)}
    (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(summary)
    if summary['errors'] or summary['intent_matches']!=len(rows):
        raise RuntimeError('routing_acceptance_failed_see_results')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cases',type=Path,default=CASES)
    args=parser.parse_args()
    asyncio.run(run(args.output,args.cases))
