"""T5 topic development check; remote models use synthetic tools, never live services."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from datetime import datetime, timezone

import httpx
from rank_bm25 import BM25Okapi

from .agent import SYSTEM, diagnose
from .config import ROOT, load_environment
from .corpus import policy_digest
from .evaluate import AblationRetriever, MeasuredProviders, ReplayTools, digest, read
from .retrieval import INDEX, tokenize

DATA = ROOT / 'evaluation/knowledge_t5'


class TopicRetriever(AblationRetriever):
    def __init__(self, provider, path, mode, *, expanded=True):
        super().__init__(provider, path, mode)
        if not expanded:
            keep = [i for i, c in enumerate(self.chunks) if not c['path'].startswith('docs/knowledge/')]
            self.chunks = [self.chunks[i] for i in keep]
            self.vectors = self.vectors[keep]
            self.bm25 = BM25Okapi([tokenize(c['heading']+' '+c['text']) or ['_'] for c in self.chunks])


async def run(args):
    load_environment()
    if args.output.exists():
        raise ValueError('topic_output_exists_use_new_directory')
    cases, fixtures = read(DATA/'cases.json'), read(DATA/'fixtures.json')
    mode = 'hybrid_rerank' if args.remote else 'bm25'
    async with httpx.AsyncClient(trust_env=False) as client:
        current = TopicRetriever(MeasuredProviders(client), args.index, mode)
        if not current.chunks or any(digest(ROOT/c['path']) != c['sha256'] for c in current.chunks):
            raise ValueError('topic_index_missing_or_stale')
        manifest = {'started_at': datetime.now(timezone.utc).isoformat(),
                    'scope': 'six T5 development cases; synthetic runtime replay, not live faults or independent accuracy',
                    'mode': mode, 'index_sha256': digest(args.index), 'policy_sha256': policy_digest(),
                    'prompt_sha256': __import__('hashlib').sha256(SYSTEM.encode()).hexdigest(),
                    'data': {p: digest(DATA/p) for p in ('cases.json', 'fixtures.json', 'labels.json', 'sources.json')},
                    'files': {p: digest(ROOT/p) for p in ('diagnosis/evaluate_knowledge.py', 'diagnosis/evaluate.py',
                        'diagnosis/agent.py', 'diagnosis/providers.py', 'diagnosis/routing.py',
                        'diagnosis/retrieval.py', 'diagnosis/corpus.py')},
                    'models': {key: os.getenv(key, default) for key, default in (
                        ('DEEPSEEK_MODEL', 'deepseek-flash'), ('EMBEDDING_MODEL', 'Qwen/Qwen3-Embedding-8B'),
                        ('RERANK_MODEL', 'Qwen/Qwen3-Reranker-8B'))},
                    'comparison': 'same code, ranking parameters and query; before excludes only docs/knowledge and rebuilds BM25; after includes them'}
        args.output.mkdir(parents=True)
        (args.output/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
        rows = []
        for case in cases:
            provider = MeasuredProviders(client)
            for phase in ('before', 'after', *(['answer'] if args.remote else [])):
                start, call_start = time.monotonic(), len(provider.calls)
                retriever = TopicRetriever(provider, args.index, mode, expanded=phase != 'before')
                row = {'id': case['id'], 'phase': phase}
                try:
                    async with asyncio.timeout(120):
                        if phase == 'answer':
                            tools = ReplayTools(client, retriever, fixtures[case['fixture']])
                            report = await diagnose(case['question'], provider, tools)
                            sources = {e['source'] for e in report['evidence']}
                            row.update(report=report, tools=tools.calls,
                                route_matches=report['kind'] == case['kind'],
                                required_sources_present=('technical_docs' in sources if case['kind'] == 'knowledge'
                                                          else 'workers' in sources),
                                semantic_review='pending_manual_review')
                        else:
                            chunks = await retriever.search(case['question'])
                            row.update(results=chunks, new_note_hit_at_5=any(c['path'] in case['notes'] for c in chunks),
                                returned_sources=[c['path'] for c in chunks])
                    row['status'] = 'ok'
                except Exception as exc:
                    row.update(status='error', error=type(exc).__name__+':'+str(exc))
                row.update(seconds=round(time.monotonic()-start, 4), calls=provider.calls[call_start:],
                           trace=(tools.retriever.traces if phase == 'answer' else retriever.traces))
                rows.append(row)
                with (args.output/'results.jsonl').open('a') as output:
                    output.write(json.dumps(row, ensure_ascii=False)+'\n')
                print(case['id'], phase, row['status'], row.get('new_note_hit_at_5'), row.get('route_matches'), flush=True)
                if row['status'] == 'error':
                    raise RuntimeError('topic_evaluation_failed_see_results')
        answers = [r for r in rows if r['phase'] == 'answer']
        summary = {'scope': manifest['scope'], 'mode': mode, 'cases': len(cases),
                   'new_note_hit_at_5': {phase: sum(r.get('new_note_hit_at_5', False) for r in rows if r['phase'] == phase)
                                        for phase in ('before', 'after')},
                   'answers': len(answers), 'route_matches': sum(r['route_matches'] for r in answers),
                   'required_sources_present': sum(r['required_sources_present'] for r in answers),
                   'api_calls': sum(len(r['calls']) for r in rows),
                   'semantic_review': 'pending_manual_review' if answers else 'not_run'}
        (args.output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=Path, default=INDEX)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--remote', action='store_true', help='Paid retrieval and six answer tasks; runtime evidence is synthetic')
    asyncio.run(run(parser.parse_args()))
