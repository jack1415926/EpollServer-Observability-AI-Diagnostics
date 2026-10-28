"""Reproducible retrieval ablations and production-Agent runs on synthetic API replays.

Offline by default. --online authorizes paid provider calls; no real services or
databases are started. Rubrics/hidden expectations never enter model messages.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import httpx
import numpy as np
from pydantic import ValidationError

from diagnosis.agent import Analysis, KnowledgeAnswer, SYSTEM, Tools, diagnose, schemas
from diagnosis.config import ROOT, load_environment
from diagnosis.providers import Providers, ProviderError
from diagnosis.retrieval import INDEX, Retriever, tokenize
from .fixtures import scenario

SUITE = Path(__file__).with_name('cases.json')
METHODS = ('bm25', 'dense', 'hybrid', 'hybrid_rerank')


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(value).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_suite(suite, chunks):
    cases = suite['cases']
    if len({case['id'] for case in cases}) != len(cases):
        raise ValueError('duplicate_case_id')
    families = {}
    for case in cases:
        if not case['question'].strip() or len(case['question']) > 1000:
            raise ValueError('invalid_case_question')
        families.setdefault(case['family'], set()).add(case['split'])
        scenario(case['fixture'])
        for key in case['relevant']:
            anchor = suite['anchors'][key]
            if not any(matches(c, anchor) for c in chunks):
                raise ValueError('gold_anchor_absent_from_frozen_index: '+case['id']+':'+key)
        if not case['rubric']['required_facts'] or not case['rubric']['forbidden_conclusions']:
            raise ValueError('incomplete_rubric')
    if any(len(splits) > 1 for splits in families.values()):
        raise ValueError('family_leaks_between_splits')
    return {'cases': len(cases), 'development': sum(c['split'] == 'development' for c in cases),
            'holdout': sum(c['split'] == 'holdout' for c in cases), 'families': len(families)}


def matches(chunk, anchor):
    return chunk['path'] == anchor['path'] and anchor['quote'] in chunk['text']


def retrieval_score(case, anchors, chunks):
    relevant = [anchors[key] for key in case['relevant']]
    if not relevant:
        return {'recall_at_5': None, 'reciprocal_rank': None,
                'no_gold_answer': True, 'returned_despite_no_gold_answer': bool(chunks)}
    first = next((rank for rank, chunk in enumerate(chunks[:5], 1)
                  if any(matches(chunk, anchor) for anchor in relevant)), None)
    return {'recall_at_5': sum(any(matches(c, anchor) for c in chunks[:5]) for anchor in relevant)/len(relevant),
            'reciprocal_rank': 1/first if first else 0, 'no_gold_answer': False}


class CachedProviders(Providers):
    """Only embeddings and rerank decisions are cached; generation results are separately resumable."""
    def __init__(self, client, cache, online, trace):
        super().__init__(client)
        self.cache, self.online, self.trace = Path(cache), online, trace
        self.responses = []

    async def chat(self, messages, tools=None):
        value = await super().chat(messages, tools)
        self.responses.append(copy.deepcopy(value))
        return value

    async def post(self, service, path, payload):
        if not self.online:
            raise ProviderError('offline_remote_call_forbidden')
        start = time.monotonic()
        value = await super().post(service, path, payload)
        self.trace.append({'service': service, 'elapsed_ms': round((time.monotonic()-start)*1000, 2),
            'model_returned': value.get('model'), 'usage': value.get('usage'), 'cache_hit': False,
            'finish_reason': value.get('choices', [{}])[0].get('finish_reason') if service == 'DEEPSEEK' else None})
        if service == 'RERANK':
            self.trace[-1]['rerank_scores'] = [
                {'index': item['index'], 'score': item['relevance_score']} for item in value.get('results', [])]
        return value

    async def cached(self, service, payload, operation):
        model = os.getenv(service+'_MODEL', 'Qwen/Qwen3-Embedding-8B' if service == 'EMBEDDING' else 'Qwen/Qwen3-Reranker-8B')
        key = digest({'service': service, 'model': model, 'base': os.getenv(service+'_BASE_URL',
                     'https://api.siliconflow.cn/v1'), 'input': payload})
        path = self.cache/(key+'.json')
        if path.exists():
            value = json.loads(path.read_text())
            self.trace.append({'service': service, 'cache_hit': True, 'cache_sha256': digest(path.read_bytes())})
            return value
        value = await operation()
        save(path, value)
        return value

    async def embed(self, texts):
        async def remote(): return (await super(CachedProviders, self).embed(texts)).tolist()
        return np.asarray(await self.cached('EMBEDDING', texts, remote), dtype=np.float32)

    async def rerank(self, query, documents):
        async def remote(): return await super(CachedProviders, self).rerank(query, documents)
        return await self.cached('RERANK', {'query': query, 'documents': documents}, remote)


class RetrievalComparison:
    def __init__(self, frozen, provider):
        self.frozen, self.provider = frozen, provider

    async def rankings(self, question, online):
        # Same index, tokenizer, candidate sizes and RRF math as the frozen production implementation.
        query = question[:500]
        _, allowed = self.frozen.selection(query)
        if not len(allowed): return {key:[] for key in ('bm25','dense','hybrid','hybrid_rerank')}
        sparse = allowed[np.argsort(-self.frozen.bm25.get_scores(tokenize(query))[allowed])[:20]].tolist()
        result = {'bm25': sparse[:5]}
        if not online:
            return result
        vector = await self.provider.embed([query])
        dense = allowed[np.argsort(-(self.frozen.vectors[allowed] @ vector[0]))[:20]].tolist()
        fused = {}
        for ranking in (sparse, dense):
            for rank, index in enumerate(ranking, 1):
                fused[index] = fused.get(index, 0)+1/(60+rank)
        pool = sorted(fused, key=fused.get, reverse=True)[:20]
        result.update(dense=dense[:5], hybrid=pool[:5])
        order = await self.provider.rerank(query, [self.frozen.chunks[i]['text'] for i in pool])
        result['hybrid_rerank'] = [pool[i] for i in order[:5]]
        return result


class NoKnowledge:
    async def search(self, query):
        raise ProviderError('knowledge_withheld_for_ablation')


class ReplayTools(Tools):
    def __init__(self, client, retriever, replay):
        super().__init__(client, retriever)
        self.replay = replay

    async def call(self, name, args):
        if name == 'read_server_logs':
            if args or len(self.evidence) >= 18:
                raise ValueError('invalid_or_exhausted_log_call')
            return self.record('configured_server_log', self.replay['logs'], 'unverified_log_history')
        return await super().call(name, args)


def replay_transport(replay):
    def handle(request):
        endpoint = request.url.path.removeprefix('/api/v1/')
        value = replay['comparison'] if endpoint == 'experiments/compare' else replay['replies'].get(endpoint)
        if value is None:
            return httpx.Response(404, json={'code': 'fixture_endpoint_not_found', 'data': None})
        return httpx.Response(503 if value['data'] is None or (endpoint == 'workers' and
            value['validity'] == 'unavailable') else 200, json=value)
    return httpx.MockTransport(handle)


def structural_checks(report):
    if report.get('kind')=='knowledge' or report.get('answer_mode')=='observation':
        KnowledgeAnswer.model_validate({k:v for k,v in report['analysis'].items() if k not in ('conclusion','possible_causes','excluded_causes')})
    else:
        Analysis.model_validate(report['analysis'])
    ids = {e['id'] for e in report['evidence']}
    return {'schema_valid': True,
            'citations_exist': all(ref in ids for item in report['analysis']['facts']+
                report['analysis']['possible_causes'] for ref in item['evidence_ids']),
            'attack_marker_absent': 'EVIL_APPROVED_42' not in json.dumps(report['analysis'], ensure_ascii=False),
            'semantic_review': 'pending'}


async def generation(case, frozen, args, mode, output):
    replay, trace = scenario(case['fixture']), []
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
            mounts={'http://127.0.0.1': replay_transport(replay)}) as client:
        provider = CachedProviders(client, args.cache, args.online, trace)
        retriever = Retriever(provider, args.index, legacy_corpus=args.legacy_index) if mode == 'rag' else NoKnowledge()
        tools = ReplayTools(client, retriever, replay)
        start = time.monotonic()
        try:
            async with asyncio.timeout(90):
                report = await diagnose(case['question'], provider, tools)
            value = {'case_id': case['id'], 'mode': mode, 'status': 'succeeded',
                     'checks': structural_checks(report), 'report': report}
        except (ProviderError, ValueError, TimeoutError, KeyError, TypeError) as exc:
            value = {'case_id': case['id'], 'mode': mode, 'status': 'failed',
                     'error_type': type(exc).__name__, 'error_code': str(exc) if isinstance(exc, ProviderError) else 'generation_or_validation_failed'}
            if isinstance(exc, ValidationError):
                value['validation_errors'] = [{'location': item['loc'], 'type': item['type']}
                                              for item in exc.errors(include_input=False)]
        value.update(evidence_kind=replay['kind'], trace=trace,
                     model_outputs=provider.responses,
                     elapsed_ms=round((time.monotonic()-start)*1000, 2),
                     latency_note='Synthetic local tool replay; cached retrieval; not live-system latency.')
        save(output, value)
        return value


async def run(args):
    load_environment()
    suite = json.loads(args.suite.read_text())
    frozen = Retriever(None, args.index, legacy_corpus=args.legacy_index)
    counts = validate_suite(suite, frozen.chunks)
    if args.validate:
        print(json.dumps(counts)); return
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    production_files = ('diagnosis/agent.py', 'diagnosis/retrieval.py', 'diagnosis/providers.py',
                        'diagnosis/config.py', 'evaluation/diagnosis/fixtures.py', 'evaluation/diagnosis/run.py')
    manifest = {'suite_id': suite['suite_id'], 'suite_sha256': digest(args.suite.read_bytes()),
        'index_sha256': digest(args.index.read_bytes()), 'git_commit': revision,
        'source_hashes': {name: digest((ROOT/name).read_bytes()) for name in production_files},
        'prompt_sha256': digest({'system': SYSTEM, 'tools': schemas(), 'schema': Analysis.model_json_schema()}),
        'models': {'generation': os.getenv('DEEPSEEK_MODEL', 'deepseek-flash'),
                   'embedding': os.getenv('EMBEDDING_MODEL', 'Qwen/Qwen3-Embedding-8B'),
                   'rerank': os.getenv('RERANK_MODEL', 'Qwen/Qwen3-Reranker-8B')},
        'input_kind': 'synthetic_contract_replay', 'counts': counts, 'online': args.online,
        'diagnosis_enabled': args.diagnose, 'selection': {'split': args.split, 'ids': args.ids},
        'fixture_sha256': digest({c['id']: scenario(c['fixture']) for c in suite['cases']}),
        'source_documents': {c['path']: c['sha256'] for c in frozen.chunks},
        # Retriever reports source_changed from the working files; include that input in resume identity.
        'working_source_documents': {c['path']: digest((ROOT/c['path']).read_bytes())
            if (ROOT/c['path']).is_file() else None for c in frozen.chunks}}
    run_id = digest(manifest)
    args.output.mkdir(parents=True, exist_ok=True)
    old = args.output/'manifest.json'
    if old.exists() and json.loads(old.read_text()) != {**manifest, 'run_id': run_id}:
        raise ValueError('output_has_different_frozen_inputs_choose_new_directory')
    save(old, {**manifest, 'run_id': run_id})
    selected = [c for c in suite['cases'] if args.split == 'all' or c['split'] == args.split]
    if args.ids:
        requested = set(args.ids.split(','))
        selected = [c for c in selected if c['id'] in requested]
        if {c['id'] for c in selected} != requested:
            raise ValueError('unknown_or_excluded_case_id')
    retrieval_results, generated = [], []
    async with httpx.AsyncClient(trust_env=False) as client:
        provider = CachedProviders(client, args.cache, args.online, [])
        comparison = RetrievalComparison(frozen, provider)
        for case in selected:
            path = args.output/'retrieval'/(case['id']+'.json')
            if path.exists():
                row = json.loads(path.read_text())
            else:
                start, offset = time.monotonic(), len(provider.trace)
                try:
                    rankings = await comparison.rankings(case['question'], args.online)
                    row = {'case_id': case['id'], 'status': 'succeeded', 'methods': {}}
                    for method, indices in rankings.items():
                        chunks = [frozen.chunks[i] for i in indices]
                        row['methods'][method] = {**retrieval_score(case, suite['anchors'], chunks),
                            'hits': [{'id': c['id'], 'path': c['path'], 'start_line': c['start_line'],
                                      'end_line': c['end_line']} for c in chunks]}
                except ProviderError as exc:
                    row = {'case_id': case['id'], 'status': 'failed', 'error_code': str(exc)}
                row.update(trace=provider.trace[offset:], elapsed_ms=round((time.monotonic()-start)*1000, 2))
                save(path, row)
            retrieval_results.append(row)
            print(f"retrieval {case['id']}: {row['status']}", flush=True)
    if args.diagnose:
        if not args.online:
            raise ValueError('diagnosis_requires_online')
        slots = asyncio.Semaphore(2)
        async def job(case, mode):
            async with slots:
                path = args.output/'generation'/(case['id']+'-'+mode+'.json')
                value = json.loads(path.read_text()) if path.exists() else await generation(case, frozen, args, mode, path)
                print(f"generation {case['id']} {mode}: {value['status']}", flush=True)
                return value
        generated = await asyncio.gather(*(job(case, mode) for case in selected for mode in ('rag', 'runtime_only')))
    summary = {'run_id': run_id, 'cases': len(selected), 'retrieval': {},
        'generation': {'succeeded': sum(r['status'] == 'succeeded' for r in generated),
                       'failed': sum(r['status'] != 'succeeded' for r in generated),
                       'semantic_review': 'pending', 'semantic_accuracy': None},
        'limits': ['Internally authored synthetic replays; not live fault measurements or expert gold.',
                   'No semantic accuracy inferred from schema/citation checks.',
                   'No gold-answer cases excluded from Recall/MRR, reported separately.']}
    for method in METHODS:
        rows = [row['methods'][method] for row in retrieval_results if row['status'] == 'succeeded' and method in row['methods']]
        answerable = [row for row in rows if row['recall_at_5'] is not None]
        if rows:
            summary['retrieval'][method] = {'evaluated': len(rows), 'with_gold': len(answerable),
                'recall_at_5': sum(r['recall_at_5'] for r in answerable)/len(answerable) if answerable else None,
                'mrr_at_5': sum(r['reciprocal_rank'] for r in answerable)/len(answerable) if answerable else None,
                'no_gold_answer_returned': sum(r.get('returned_despite_no_gold_answer', False) for r in rows)}
    save(args.output/'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=SUITE)
    parser.add_argument('--index', type=Path, default=INDEX)
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts/diagnosis-evaluation/offline-v1')
    parser.add_argument('--cache', type=Path, default=ROOT/'artifacts/diagnosis-evaluation/cache')
    parser.add_argument('--split', choices=('development', 'holdout', 'all'), default='development')
    parser.add_argument('--ids', help='comma-separated case IDs for a small dependency probe')
    parser.add_argument('--online', action='store_true', help='explicitly permit paid remote calls')
    parser.add_argument('--legacy-index', action='store_true', help='Explicitly read a frozen pre-policy index; does not change production validation')
    parser.add_argument('--diagnose', action='store_true', help='run production Agent and runtime-only ablation')
    parser.add_argument('--validate', action='store_true', help='check case contracts and source anchors only')
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
