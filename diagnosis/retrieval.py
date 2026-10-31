"""Heading chunks, BM25 + normalized dense recall, RRF and remote reranking.

Adapted methods from KBrag-V6 at 472a69940e61fd38c70d12c727419286e24742a8;
the product-manual data, router and prompts are deliberately independent.
"""
import argparse
import copy
import io
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import httpx
import numpy as np
from rank_bm25 import BM25Okapi

from .config import ROOT, load_environment, redact
from .providers import Providers, ProviderError
from .corpus import annotate, eligible, load_policy, markdown_headings, policy_digest, search_purpose
from .catalog import CATALOG, documents as catalog_documents

INDEX = ROOT / 'artifacts/diagnosis/index.npz'


def tokenize(text):
    words = re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]+', text.lower())
    return [token for word in words for token in
            ([word[i:i+2] for i in range(len(word)-1)] if re.match(r'[\u4e00-\u9fff]', word) and len(word) > 1 else [word])]


def documents(root=ROOT, corpus='current'):
    if corpus in ('catalog', 'legacy'):
        return catalog_documents(root, 'current' if corpus == 'catalog' else 'legacy')
    if corpus != 'current':
        raise ValueError('invalid_knowledge_corpus')
    policy = load_policy(root)
    paths = [root / 'README.md', root / 'docs/DECISIONS.md', root / 'docs/DEVELOPMENT_ISSUES.md']
    paths.append(root / 'docs/T7_WORKER_RESOURCES.md')
    paths.append(root / 'docs/TCP_ACTIVE_PROBE.md')
    paths += sorted((root / 'docs').glob('STAGE_*.md'))
    paths += sorted((root / 'docs/upstream').glob('*.md'))
    paths += sorted((root / 'docs/knowledge').glob('*.md'))
    if {p.relative_to(root).as_posix() for p in paths} != set(policy['sources']):
        raise ValueError('corpus_sources_require_review')
    chunks = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        raw = path.read_text(encoding='utf-8')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        lines = raw.splitlines()
        start, heading, section, hierarchy = 1, path.stem, [], []

        def emit():
            # Line-based windows preserve a verifiable source span; oversized lines are bounded.
            cuts = {0, len(section)}
            for annotation in policy['sources'][relative].get('spans', []):
                for boundary in (annotation['start_line'], annotation['end_line'] + 1):
                    if start < boundary < start + len(section):
                        cuts.add(boundary - start)
            boundaries = sorted(cuts)
            for begin, end in zip(boundaries, boundaries[1:]):
                for offset in range(begin, end, 18):
                    span = section[offset:min(offset+24, end)]
                    content = redact('\n'.join(span))[:3500]
                    if not content.strip():
                        continue
                    a, b = start + offset, start + offset + len(span) - 1
                    key = hashlib.sha256(f'{relative}:{digest}:{a}:{b}'.encode()).hexdigest()[:20]
                    metadata = annotate(policy, relative, [title for _, title in hierarchy], a)
                    chunks.append({'id': key, 'path': relative, 'start_line': a, 'end_line': b,
                        'sha256': digest, 'heading': heading, 'text': content,
                        'scope': ('primary_reference' if metadata['source_quality'] == 'primary_manual_summary' else
                                  'historical_reference' if '/upstream/' in relative else 'project_document'),
                        'corpus': metadata})

        headings = {number: (level, title) for number, level, title in markdown_headings(raw)}
        for number, line in enumerate(lines, 1):
            if number in headings:
                emit()
                level, heading = headings[number]
                start, section = number, []
                hierarchy = [(depth, title) for depth, title in hierarchy if depth < level]
                hierarchy.append((level, heading))
            section.append(line)
        emit()
    return chunks


class Retriever:
    def __init__(self, providers, path=INDEX, *, legacy_corpus=False):
        self.providers = providers
        self.legacy_corpus = legacy_corpus
        self.chunks = []
        self.traces = []
        self.index_sha256 = self.policy_sha256 = None
        self.corpus = 'current'
        if Path(path).exists():
            raw = Path(path).read_bytes()
            self.index_sha256 = hashlib.sha256(raw).hexdigest()
            with np.load(io.BytesIO(raw), allow_pickle=False) as bundle:
                self.chunks = json.loads(str(bundle['metadata']))
                self.vectors = bundle['vectors']
                self.model = str(bundle['model'])
                self.corpus = str(bundle['corpus']) if 'corpus' in bundle else ('legacy' if legacy_corpus else 'current')
                self.policy_sha256 = str(bundle['corpus_policy_sha256']) if 'corpus_policy_sha256' in bundle else None
                if self.corpus == 'catalog' and not legacy_corpus:
                    expected = hashlib.sha256((ROOT/CATALOG).read_bytes()).hexdigest()
                    if ('catalog_sha256' not in bundle or str(bundle['catalog_sha256']) != expected or
                            any(c.get('provenance', {}).get('catalog_sha256') != expected for c in self.chunks)):
                        raise ValueError('knowledge_index_catalog_outdated_rebuild')
                    self.policy_sha256 = expected
                    self.legacy_corpus = True
                elif not legacy_corpus and ('corpus_policy_sha256' not in bundle or
                        str(bundle['corpus_policy_sha256']) != policy_digest()):
                    raise ValueError('knowledge_index_policy_outdated_rebuild')
            if (self.model != os.getenv('EMBEDDING_MODEL', 'Qwen/Qwen3-Embedding-8B') or
                    self.vectors.ndim != 2 or len(self.chunks) != len(self.vectors) or
                    not self.chunks or not np.isfinite(self.vectors).all()):
                raise ValueError('invalid_diagnostic_index')
            self.bm25 = BM25Okapi([tokenize(c['heading'] + ' ' + c['text']) or ['_'] for c in self.chunks])

    def for_task(self, provider):
        context = copy.copy(self)
        context.providers, context.traces = provider, []
        return context

    def selection(self, query):
        purpose = 'legacy' if self.legacy_corpus else search_purpose(query)
        indices = [i for i, chunk in enumerate(self.chunks) if self.legacy_corpus or eligible(chunk, purpose)]
        return purpose, np.asarray(indices, dtype=int)

    async def search(self, query):
        if not self.chunks:
            raise ProviderError('knowledge_index_missing')
        purpose, allowed = self.selection(query)
        if not len(allowed):
            return []
        dense = await self.providers.embed([query])
        if dense.shape[1] != self.vectors.shape[1]:
            raise ProviderError('embedding_dimension_changed')
        sparse_scores = self.bm25.get_scores(tokenize(query))
        rankings = [allowed[np.argsort(-sparse_scores[allowed])[:20]],
                    allowed[np.argsort(-(self.vectors[allowed] @ dense[0]))[:20]]]
        scores = {}
        for ranking in rankings:
            for rank, index in enumerate(ranking, 1):
                scores[int(index)] = scores.get(int(index), 0) + 1 / (60 + rank)
        candidates = sorted(scores, key=scores.get, reverse=True)[:20]
        order = await self.providers.rerank(query, [self.chunks[i]['text'] for i in candidates])
        results = []
        for position in order[:5]:
            chunk = dict(self.chunks[candidates[position]])
            if not self.legacy_corpus:
                chunk['retrieval_purpose'] = purpose
            source = ROOT / chunk['path']
            chunk['source_changed'] = (not source.exists() or
                hashlib.sha256(source.read_bytes()).hexdigest() != chunk['sha256'])
            supporting = chunk.get('provenance', {}).get('local_source_hashes', {})
            chunk['supporting_source_changed'] = any(not (ROOT/path).is_file() or
                hashlib.sha256((ROOT/path).read_bytes()).hexdigest() != value for path, value in supporting.items())
            catalog_hash = chunk.get('provenance', {}).get('catalog_sha256')
            chunk['catalog_changed'] = bool(catalog_hash and (not (ROOT/CATALOG).is_file() or
                hashlib.sha256((ROOT/CATALOG).read_bytes()).hexdigest() != catalog_hash))
            results.append(chunk)
        self.traces.append({'query': redact(query), 'purpose': purpose,
            'bm25': [{'id': self.chunks[int(i)]['id'], 'score': float(sparse_scores[int(i)])} for i in rankings[0]],
            'dense': [{'id': self.chunks[int(i)]['id'], 'score': float(self.vectors[int(i)] @ dense[0])} for i in rankings[1]],
            'candidates': [{'id': self.chunks[i]['id'], 'rrf_score': scores[i]} for i in candidates],
            'rerank': [{'id': self.chunks[candidates[i]]['id'], 'score': getattr(order, 'scores', {}).get(i)} for i in order],
            'returned': [c['id'] for c in results]})
        return results


async def build(path=INDEX, *, offline=False, corpus='current'):
    load_environment()
    chunks = documents() if corpus == 'current' else documents(corpus=corpus)
    if not chunks:
        raise ValueError('empty_corpus')
    path = Path(path)
    model = os.getenv('EMBEDDING_MODEL', 'Qwen/Qwen3-Embedding-8B')
    cached = {}
    if path.exists():
        with np.load(path, allow_pickle=False) as old:
            if str(old['model']) == model:
                cached = {c['heading']+'\n'+c['text']: v for c, v in
                          zip(json.loads(str(old['metadata'])), old['vectors'])}
    texts = [c['heading']+'\n'+c['text'] for c in chunks]
    vectors = [cached.get(text) for text in texts]
    missing = [i for i, vector in enumerate(vectors) if vector is None]
    if offline and missing:
        raise ValueError(f'corpus_build_requires_embedding: {len(missing)} chunks; index unchanged')
    async with httpx.AsyncClient(trust_env=False) as client:
        provider = Providers(client)
        for offset in range(0, len(missing), 8):
            indices = missing[offset:offset+8]
            batch = await provider.embed([texts[i] for i in indices])
            for index, vector in zip(indices, batch):
                vectors[index] = vector
            print(f'Embedded {min(offset+8, len(missing))}/{len(missing)} new chunks', flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.npz')
    try:
        metadata = {'corpus_policy_sha256': policy_digest()} if corpus == 'current' else (
            {'catalog_sha256': hashlib.sha256((ROOT/CATALOG).read_bytes()).hexdigest()} if corpus == 'catalog' else {})
        with os.fdopen(fd, 'wb') as output:
            np.savez_compressed(output, vectors=np.stack(vectors),
                metadata=json.dumps(chunks, ensure_ascii=False),
                model=model, corpus=corpus, **metadata)
        os.replace(temporary, path)
        print(f'Published {len(chunks)} chunks; reused {len(chunks)-len(missing)} vectors', flush=True)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Build the project-only diagnostic knowledge index')
    parser.add_argument('--output', type=Path, default=INDEX)
    parser.add_argument('--offline', action='store_true', help='Reuse cached vectors only; fail before API calls if any text is new')
    parser.add_argument('--corpus', choices=('current', 'catalog', 'legacy'), default='current')
    args = parser.parse_args()
    asyncio.run(build(args.output, offline=args.offline, corpus=args.corpus))
