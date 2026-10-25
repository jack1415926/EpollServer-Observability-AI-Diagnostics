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
CATALOG = Path('diagnosis/knowledge/catalog.json')


def tokenize(text):
    words = re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]+', text.lower())
    return [token for word in words for token in
            ([word[i:i+2] for i in range(len(word)-1)] if re.match(r'[\u4e00-\u9fff]', word) and len(word) > 1 else [word])]


def documents(root=ROOT, corpus='current'):
    root = Path(root)
    provenance = {}
    if corpus == 'legacy':
        paths = [root / 'README.md', root / 'docs/DECISIONS.md', root / 'docs/DEVELOPMENT_ISSUES.md']
        paths += sorted((root / 'docs').glob('STAGE_*.md'))
        paths += sorted((root / 'docs/upstream').glob('*.md'))
    elif corpus == 'current':
        catalog_bytes = (root / CATALOG).read_bytes()
        catalog = json.loads(catalog_bytes)
        if catalog.get('schema_version') != 1:
            raise ValueError('invalid_knowledge_catalog')
        paths = []
        for entry in catalog['entries']:
            relative = Path(entry['path'])
            path = root / relative
            if (relative.is_absolute() or '..' in relative.parts or relative.suffix != '.md' or
                    not path.resolve().is_relative_to((root / 'diagnosis/knowledge').resolve()) or
                    entry['kind'] not in ('current_contract', 'verified_incident', 'technical_reference') or
                    not entry['revision'] or not entry['authority'] or not entry['applies_to'] or
                    not path.is_file() or path.is_symlink() or entry['path'] in provenance):
                raise ValueError('invalid_knowledge_entry')
            local = {}
            for source in entry['sources']:
                if source.startswith('https://'):
                    continue
                ref = Path(source)
                if ref.is_absolute() or '..' in ref.parts or not (root/ref).is_file():
                    raise ValueError('invalid_knowledge_supporting_source')
                local[source] = hashlib.sha256((root/ref).read_bytes()).hexdigest()
            provenance[entry['path']] = {**entry, 'catalog_revision': catalog['revision'],
                                         'catalog_sha256': hashlib.sha256(catalog_bytes).hexdigest(),
                                         'local_source_hashes': local}
            paths.append(path)
    else:
        raise ValueError('invalid_knowledge_corpus')
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
                metadata = provenance.get(relative)
                chunks.append({'id': key, 'path': relative, 'start_line': a, 'end_line': b,
                    'sha256': digest, 'heading': heading, 'text': content,
                    'scope': metadata['kind'] if metadata else
                        'historical_reference' if '/upstream/' in relative else 'project_document',
                    **({'provenance': metadata} if metadata else {})})

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
                self.corpus = str(bundle['corpus']) if 'corpus' in bundle else 'legacy'
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
            supporting = chunk.get('provenance', {}).get('local_source_hashes', {})
            chunk['supporting_source_changed'] = any(not (ROOT/path).is_file() or
                hashlib.sha256((ROOT/path).read_bytes()).hexdigest() != value for path, value in supporting.items())
            catalog_hash = chunk.get('provenance', {}).get('catalog_sha256')
            chunk['catalog_changed'] = bool(catalog_hash and (not (ROOT/CATALOG).is_file() or
                hashlib.sha256((ROOT/CATALOG).read_bytes()).hexdigest() != catalog_hash))
            results.append(chunk)
        return results


async def build(path=INDEX, corpus='current'):
    load_environment()
    chunks = documents(corpus=corpus)
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
                model=model, corpus=corpus)
        os.replace(temporary, path)
        print(f'Published {len(chunks)} chunks; reused {len(chunks)-len(missing)} vectors', flush=True)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Build the project-only diagnostic knowledge index')
    parser.add_argument('--output', type=Path, default=INDEX)
    parser.add_argument('--corpus', choices=('current', 'legacy'), default='current')
    args = parser.parse_args()
    asyncio.run(build(args.output, args.corpus))
