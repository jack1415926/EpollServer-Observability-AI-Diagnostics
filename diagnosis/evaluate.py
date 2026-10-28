"""Frozen-corpus retrieval ablations and Agent replay; never reads live services.

Gold labels are used only after generation. Raw results stay in ignored artifacts.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import platform
import time
from datetime import datetime, timezone

import httpx
import numpy as np

from .agent import SYSTEM, Tools, diagnose
from .config import ROOT, load_environment
from .providers import Providers
from .retrieval import INDEX, Retriever, tokenize

DATA = ROOT / 'evaluation/diagnosis_v1'
MODES = ('bm25', 'dense', 'hybrid', 'hybrid_rerank')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def retrieval_metrics(results, groups):
    """Recall of annotated evidence groups (alternative spans), MRR of first hit."""
    if not groups:
        return {'recall_at_5': None, 'mrr_at_5': None}
    def hit(chunk, group):
        return any(chunk['path'] == span['path'] and
                   chunk['start_line'] <= span['line'] <= chunk['end_line'] for span in group)
    top = results[:5]
    ranks = [i for i, chunk in enumerate(top, 1) if any(hit(chunk, g) for g in groups)]
    return {'recall_at_5': sum(any(hit(c, g) for c in top) for g in groups) / len(groups),
            'mrr_at_5': 1 / min(ranks) if ranks else 0.0}


class MeasuredProviders(Providers):
    """Record provider metadata, not request bodies or credentials; cache per case only."""
    def __init__(self, client):
        super().__init__(client)
        self.calls, self.embeddings, self.reranks = [], {}, {}

    async def post(self, service, path, payload):
        entry = {'service': service, 'requested_model': payload['model']}
        start = time.monotonic()
        try:
            data = await super().post(service, path, payload)
            entry.update(model=data.get('model'), usage=data.get('usage'),
                         finish_reason=[c.get('finish_reason') for c in data.get('choices', [])])
            if path == '/responses':
                entry.update(response_status=data.get('status'), incomplete_details=data.get('incomplete_details'))
            if service == 'RERANK':
                entry['ranking'] = data.get('results')
            return data
        except Exception as exc:
            entry['error'] = type(exc).__name__ + ':' + str(exc)
            raise
        finally:
            entry['seconds'] = round(time.monotonic() - start, 4)
            self.calls.append(entry)

    async def embed(self, texts):
        key = tuple(texts)
        if key not in self.embeddings:
            self.embeddings[key] = await super().embed(texts)
        return self.embeddings[key]

    async def rerank(self, query, documents):
        key = (query, tuple(documents))
        if key not in self.reranks:
            self.reranks[key] = await super().rerank(query, documents)
        return self.reranks[key]


class AblationRetriever(Retriever):
    """Evaluation-only modes; full mode invokes the unmodified production search."""
    def __init__(self, provider, path=INDEX, mode='hybrid_rerank', *, legacy_corpus=False):
        super().__init__(provider, path, legacy_corpus=legacy_corpus)
        self.mode, self.traces = mode, []

    async def search(self, query):
        if self.mode == 'live_only':
            return []
        if not self.chunks:
            raise ValueError('evaluation_index_missing')
        purpose, allowed = self.selection(query)
        if not len(allowed):
            return []
        sparse = allowed[np.argsort(-self.bm25.get_scores(tokenize(query))[allowed])[:20]].tolist()
        trace = {'query': query, 'bm25': [self.chunks[i]['id'] for i in sparse],
                 'corpus_purpose': purpose, 'eligible_chunks': len(allowed)}
        candidates = sparse
        if self.mode != 'bm25':
            vector = await self.providers.embed([query])
            if vector.shape[1] != self.vectors.shape[1]:
                raise ValueError('embedding_dimension_changed')
            dense = allowed[np.argsort(-(self.vectors[allowed] @ vector[0]))[:20]].tolist()
            trace['dense'] = [self.chunks[i]['id'] for i in dense]
            scores = {}
            for ranking in (sparse, dense):
                for rank, index in enumerate(ranking, 1):
                    scores[index] = scores.get(index, 0) + 1 / (60 + rank)
            candidates = dense if self.mode == 'dense' else sorted(scores, key=scores.get, reverse=True)[:20]
            trace['candidates'] = [self.chunks[i]['id'] for i in candidates]
        if self.mode == 'hybrid_rerank':
            results = await super().search(query)
            trace['ranking_details'] = self.traces.pop()
        else:
            results = []
            for i in candidates[:5]:
                chunk = dict(self.chunks[i])
                if not self.legacy_corpus:
                    chunk['retrieval_purpose'] = purpose
                source = ROOT / chunk['path']
                chunk['source_changed'] = not source.exists() or digest(source) != chunk['sha256']
                results.append(chunk)
        trace['returned'] = [c['id'] for c in results]
        self.traces.append(trace)
        return results


class ReplayTools(Tools):
    def __init__(self, client, retriever, fixture):
        super().__init__(client, retriever)
        self.fixture, self.calls = fixture, []

    async def monitor(self, endpoint, payload=None):
        if endpoint == 'experiments/compare':
            expected = {'baseline': '00000000-0000-0000-0000-000000000021',
                        'candidate': '00000000-0000-0000-0000-000000000022'}
            if payload != expected:
                return {'available': False, 'error': 'fixture_experiment_not_found'}
            return self.fixture.get('compare', {'available': False, 'error': 'fixture_experiment_not_found'})
        return self.fixture[endpoint]

    async def _execute(self, name, args):
        self.calls.append({'name': name, 'args': args})
        if name == 'read_server_logs' and not args:
            if len(self.evidence) >= 18:
                raise ValueError('tool_budget_exhausted')
            return self.record('configured_server_log', self.fixture['logs'], 'unverified_log_history')
        return await super()._execute(name, args)


def automatic_checks(report, label):
    evidence = {e['id']: e for e in report['evidence']}
    claims = report['analysis']['facts'] + report['analysis']['possible_causes']
    sources = {e['source'] for e in evidence.values()}
    expected = label['expected_conclusion']
    return {'citation_ids_exist': all(ref in evidence for c in claims for ref in c['evidence_ids']),
            'required_evidence_present': all(s in sources for s in label['required_evidence']),
            'expected_conclusion_matches': None if expected is None else report['analysis']['conclusion'] == expected,
            'semantic_support': 'requires_review', 'claim_count': len(claims)}


def validate_dataset(cases, labels, fixtures):
    ids = [c['id'] for c in cases]
    if len(ids) != len(set(ids)) or set(ids) != set(labels):
        raise ValueError('case_label_ids_mismatch')
    families = {}
    for case in cases:
        if case['split'] not in ('dev', 'holdout') or not 1 <= len(case['question']) <= 1000:
            raise ValueError('invalid_case')
        if case['family'] in families and families[case['family']] != case['split']:
            raise ValueError('family_split_leakage')
        families[case['family']] = case['split']
        if case['fixture'] not in fixtures or not labels[case['id']]['required']:
            raise ValueError('missing_fixture_or_rubric')
        for group in labels[case['id']]['relevant_groups']:
            if not group:
                raise ValueError('empty_relevance_group')
            for span in group:
                path = ROOT / span['path']
                if not path.is_file() or not 1 <= span['line'] <= len(path.read_text().splitlines()):
                    raise ValueError('invalid_source_span')


async def evaluate(args):
    load_environment()
    cases, labels, fixtures = [read(DATA / (name + '.json')) for name in ('cases', 'labels', 'fixtures')]
    validate_dataset(cases, labels, fixtures)
    if args.case and args.case not in labels:
        raise ValueError('unknown_case')
    selected = [c for c in cases if (not args.case or c['id'] == args.case) and
                (args.split == 'all' or c['split'] == args.split)]
    provider_files = ['diagnosis/evaluate.py', 'diagnosis/agent.py', 'diagnosis/retrieval.py',
                      'diagnosis/providers.py', 'diagnosis/corpus.py', 'diagnosis/corpus_policy.json']
    manifest = {'dataset': 'diagnosis_v1', 'phase': args.phase, 'remote': args.remote,
                'case': args.case, 'split': args.split, 'python': platform.python_version(),
                'legacy_corpus': args.legacy_corpus,
                'index_sha256': digest(args.index), 'prompt_sha256': hashlib.sha256(SYSTEM.encode()).hexdigest(),
                'files': {p: digest(ROOT/p) for p in provider_files},
                'data': {name: digest(DATA/(name+'.json')) for name in ('cases','labels','fixtures')}}
    async with httpx.AsyncClient(trust_env=False) as client:
        retriever = Retriever(Providers(client), args.index, legacy_corpus=args.legacy_corpus)
        if not retriever.chunks:
            raise ValueError('evaluation_index_missing')
        manifest['corpus'] = {c['path']: c['sha256'] for c in retriever.chunks}
        manifest['source_files_at_run'] = {p: digest(ROOT/p) for p in manifest['corpus']}
        # Baseline provenance must be current before any paid calls.
        if manifest['corpus'] != manifest['source_files_at_run']:
            raise ValueError('evaluation_index_stale_rebuild_explicitly')
        manifest['models'] = {k: __import__('os').getenv(k, default) for k, default in (
            ('DEEPSEEK_MODEL','deepseek-flash'),('EMBEDDING_MODEL','Qwen/Qwen3-Embedding-8B'),
            ('RERANK_MODEL','Qwen/Qwen3-Reranker-8B'))}
        args.output.mkdir(parents=True, exist_ok=True)
        manifest_path = args.output/'manifest.json'
        if manifest_path.exists():
            old = read(manifest_path)
            if {k:v for k,v in old.items() if k != 'started_at'} != manifest:
                raise ValueError('resume_manifest_mismatch_use_new_output')
        else:
            manifest_path.write_text(json.dumps({**manifest, 'started_at': datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2)+'\n')
        results_path = args.output/'results.jsonl'
        done = {(r['case_id'],r['kind'],r['mode']) for r in
                (json.loads(line) for line in results_path.read_text().splitlines())} if results_path.exists() else set()
        # Sequential calls keep provider load bounded; successful rows are never charged twice on resume.
        for case in selected:
            modes = MODES if args.remote else ('bm25',)
            tasks = ([] if args.phase == 'diagnosis' else [('retrieval', m) for m in modes])
            if args.phase != 'retrieval':
                tasks += [('diagnosis', m) for m in ('hybrid_rerank','live_only')]
            provider = MeasuredProviders(client)
            for kind, mode in tasks:
                key = (case['id'],kind,mode)
                if key in done:
                    continue
                retriever = AblationRetriever(provider, args.index, mode, legacy_corpus=args.legacy_corpus)
                row = {'case_id': case['id'], 'split': case['split'], 'family': case['family'],
                       'kind':kind,'mode':mode,'origin':'synthetic_contract_replay_not_live_measurement'}
                start, call_start = time.monotonic(), len(provider.calls)
                try:
                    async with asyncio.timeout(120):
                        if kind == 'retrieval':
                            results = await retriever.search(case['question'])
                            row.update(results=results, metrics=retrieval_metrics(results, labels[case['id']]['relevant_groups']))
                        else:
                            tools = ReplayTools(client, retriever, fixtures[case['fixture']])
                            report = await diagnose(case['question'], provider, tools)
                            row.update(report=report, tools=tools.calls,
                                       checks=automatic_checks(report, labels[case['id']]))
                    row['status'] = 'ok'
                except Exception as exc:
                    row.update(status='error', error=type(exc).__name__+':'+str(exc))
                row.update(seconds=round(time.monotonic()-start,4), calls=provider.calls[call_start:],
                           trace=(tools.retriever.traces if kind == 'diagnosis' else retriever.traces))
                with results_path.open('a',encoding='utf-8') as output:
                    output.write(json.dumps(row,ensure_ascii=False)+'\n')
                print(case['id'],kind,mode,row['status'],row['seconds'],flush=True)
                if row['status'] == 'error':
                    # No automatic remote retry. Resume preserves the failed attempt for audit.
                    raise RuntimeError('evaluation_failed_see_results')
    print('Results:', results_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remote',action='store_true',help='Use paid embedding/rerank/chat APIs; default is local BM25 only')
    parser.add_argument('--phase',choices=('retrieval','diagnosis','all'),default='retrieval')
    parser.add_argument('--case',help='One-case dependency smoke check, separate output directory')
    parser.add_argument('--split',choices=('all','dev','holdout'),default='all')
    parser.add_argument('--index',type=Path,default=INDEX)
    parser.add_argument('--legacy-corpus',action='store_true',help='Explicitly replay unfiltered corpus selection for historical comparisons')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.phase != 'retrieval' and not args.remote:
        parser.error('diagnosis requires --remote')
    asyncio.run(evaluate(args))


if __name__ == '__main__':
    main()
