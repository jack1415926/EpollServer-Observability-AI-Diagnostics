"""Heading chunks, BM25 + normalized dense recall, RRF and remote reranking.

Adapted methods from KBrag-V6 at 472a69940e61fd38c70d12c727419286e24742a8;
the product-manual data, router and prompts are deliberately independent.
"""
import argparse
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

INDEX = ROOT / 'artifacts/diagnosis/index.npz'


def tokenize(text):
    words = re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]+', text.lower())
    return [token for word in words for token in
            ([word[i:i+2] for i in range(len(word)-1)] if re.match(r'[\u4e00-\u9fff]', word) and len(word) > 1 else [word])]


def documents(root=ROOT):
    paths = [root / 'README.md', root / 'docs/DECISIONS.md', root / 'docs/DEVELOPMENT_ISSUES.md']
    paths += sorted((root / 'docs').glob('STAGE_*.md'))
    paths += sorted((root / 'docs/upstream').glob('*.md'))
    chunks = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        raw = path.read_text(encoding='utf-8')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        lines = raw.splitlines()
        start, heading, section = 1, path.stem, []

        def emit():
            # Line-based windows preserve a verifiable source span; oversized lines are bounded.
            for offset in range(0, len(section), 18):
                span = section[offset:offset+24]
                content = redact('\n'.join(span))[:3500]
                if not content.strip():
                    continue
                a, b = start + offset, start + offset + len(span) - 1
                key = hashlib.sha256(f'{relative}:{digest}:{a}:{b}'.encode()).hexdigest()[:20]
                chunks.append({'id': key, 'path': relative, 'start_line': a, 'end_line': b,
                    'sha256': digest, 'heading': heading, 'text': content,
                    'scope': 'historical_reference' if '/upstream/' in relative else 'project_document'})

        for number, line in enumerate(lines, 1):
            if re.match(r'^#{1,4}\s', line):
                emit()
                start, heading, section = number, line.lstrip('# '), []
            section.append(line)
        emit()
    return chunks


class Retriever:
    def __init__(self, providers, path=INDEX):
        self.providers = providers
        self.chunks = []
        if Path(path).exists():
            with np.load(path, allow_pickle=False) as bundle:
                self.chunks = json.loads(str(bundle['metadata']))
                self.vectors = bundle['vectors']
                self.model = str(bundle['model'])
            if (self.model != os.getenv('EMBEDDING_MODEL', 'Qwen/Qwen3-Embedding-8B') or
                    self.vectors.ndim != 2 or len(self.chunks) != len(self.vectors) or
                    not self.chunks or not np.isfinite(self.vectors).all()):
                raise ValueError('invalid_diagnostic_index')
            self.bm25 = BM25Okapi([tokenize(c['heading'] + ' ' + c['text']) or ['_'] for c in self.chunks])

    async def search(self, query):
        if not self.chunks:
            raise ProviderError('knowledge_index_missing')
        dense = await self.providers.embed([query])
        if dense.shape[1] != self.vectors.shape[1]:
            raise ProviderError('embedding_dimension_changed')
        sparse_scores = self.bm25.get_scores(tokenize(query))
        rankings = [np.argsort(-sparse_scores)[:20], np.argsort(-(self.vectors @ dense[0]))[:20]]
        scores = {}
        for ranking in rankings:
            for rank, index in enumerate(ranking, 1):
                scores[int(index)] = scores.get(int(index), 0) + 1 / (60 + rank)
        candidates = sorted(scores, key=scores.get, reverse=True)[:20]
        order = await self.providers.rerank(query, [self.chunks[i]['text'] for i in candidates])
        results = []
        for position in order[:5]:
            chunk = dict(self.chunks[candidates[position]])
            source = ROOT / chunk['path']
            chunk['source_changed'] = (not source.exists() or
                hashlib.sha256(source.read_bytes()).hexdigest() != chunk['sha256'])
            results.append(chunk)
        return results


async def build(path=INDEX):
    load_environment()
    chunks = documents()
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
        with os.fdopen(fd, 'wb') as output:
            np.savez_compressed(output, vectors=np.stack(vectors),
                metadata=json.dumps(chunks, ensure_ascii=False),
                model=model)
        os.replace(temporary, path)
        print(f'Published {len(chunks)} chunks; reused {len(chunks)-len(missing)} vectors', flush=True)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Build the project-only diagnostic knowledge index')
    parser.add_argument('--output', type=Path, default=INDEX)
    args = parser.parse_args()
    asyncio.run(build(args.output))
