"""Small T4 development check: same index, unfiltered versus layered retrieval."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import httpx

from .config import ROOT, load_environment
from .corpus import eligible, policy_digest, search_purpose
from .evaluate import AblationRetriever, MeasuredProviders, digest, read
from .retrieval import INDEX

CASES = ROOT / 'evaluation/corpus_t4/cases.json'


async def run(args):
    load_environment()
    if args.output.exists():
        raise ValueError('corpus_audit_output_exists')
    cases = read(CASES)
    mode = 'hybrid_rerank' if args.remote else 'bm25'
    async with httpx.AsyncClient(trust_env=False) as client:
        provider = MeasuredProviders(client)
        current = AblationRetriever(provider, args.index, mode)
        original = AblationRetriever(provider, args.index, mode, legacy_corpus=True)
        if not current.chunks or any(digest(ROOT/c['path']) != c['sha256'] for c in current.chunks):
            raise ValueError('corpus_audit_requires_current_index')
        manifest = {'started_at': datetime.now(timezone.utc).isoformat(),
                    'scope': 'T4 development retrieval check; not diagnosis accuracy or independent holdout',
                    'mode': mode, 'index_sha256': digest(args.index), 'policy_sha256': policy_digest(),
                    'cases_sha256': digest(CASES), 'files': {p: digest(ROOT/p) for p in (
                        'diagnosis/audit_corpus.py', 'diagnosis/corpus.py', 'diagnosis/retrieval.py',
                        'diagnosis/evaluate.py', 'diagnosis/providers.py')},
                    'models': {k: __import__('os').getenv(k, default) for k, default in (
                        ('EMBEDDING_MODEL', 'Qwen/Qwen3-Embedding-8B'), ('RERANK_MODEL', 'Qwen/Qwen3-Reranker-8B'))},
                    'comparison': 'same corpus/vectors/query; only eligibility differs; query API cache shared'}
        args.output.mkdir(parents=True)
        (args.output/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
        rows = []
        for case in cases:
            purpose = search_purpose(case['question'])
            if purpose != case['expected_purpose']:
                raise ValueError('corpus_audit_purpose_mismatch')
            for name, retriever in [('unfiltered', original), ('layered', current)]:
                start, call_start = time.monotonic(), len(provider.calls)
                row = {'id': case['id'], 'selection': name, 'purpose': purpose}
                try:
                    async with asyncio.timeout(90):
                        chunks = await retriever.search(case['question'])
                    relevant = lambda c: any(c['path'] == target['path'] and
                        ('heading' not in target or c['heading'] == target['heading']) and
                        ('text' not in target or target['text'] in c['text']) for target in case['relevant'])
                    row.update(status='ok', hit_at_5=any(relevant(c) for c in chunks),
                               ineligible_returned=sum(not eligible(c, purpose) for c in chunks),
                               eligible_chunks=len(retriever.selection(case['question'])[1]), results=chunks)
                except Exception as exc:
                    row.update(status='error', error=type(exc).__name__+':'+str(exc))
                row.update(seconds=round(time.monotonic()-start, 4), calls=provider.calls[call_start:])
                rows.append(row)
                with (args.output/'results.jsonl').open('a') as output:
                    output.write(json.dumps(row, ensure_ascii=False)+'\n')
                print(case['id'], name, row['status'], row.get('hit_at_5'), row.get('ineligible_returned'), flush=True)
                if row['status'] == 'error':
                    raise RuntimeError('corpus_audit_failed_see_results')
        summary = {'scope': manifest['scope'], 'cases': len(cases), 'mode': mode,
                   'by_selection': {name: {'hit_at_5': sum(r['hit_at_5'] for r in rows if r['selection'] == name),
                       'ineligible_returned': sum(r['ineligible_returned'] for r in rows if r['selection'] == name)}
                       for name in ('unfiltered', 'layered')},
                   'regressed_cases': [case['id'] for case in cases if
                       next(r for r in rows if r['id'] == case['id'] and r['selection'] == 'unfiltered')['hit_at_5'] and
                       not next(r for r in rows if r['id'] == case['id'] and r['selection'] == 'layered')['hit_at_5']],
                   'api_calls': len(provider.calls)}
        (args.output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=Path, default=INDEX)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--remote', action='store_true', help='Use real paid Embedding/Rerank; never calls the answer model')
    asyncio.run(run(parser.parse_args()))
