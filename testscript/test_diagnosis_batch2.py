"""Knowledge provenance, bounded experiment detail and pre-start resource validation."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
import numpy as np

from diagnosis.agent import Tools, schemas
from diagnosis.retrieval import documents, Retriever

ROOT = Path(__file__).resolve().parents[1]


class Batch2Tests(unittest.IsolatedAsyncioTestCase):
    def test_catalog_index_is_explicit_and_requires_its_own_reviewed_catalog_hash(self):
        import hashlib
        from diagnosis.retrieval import CATALOG
        chunks=documents(corpus='catalog')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'index.npz'
            def write(digest):
                np.savez(path,vectors=np.ones((len(chunks),2)),metadata=json.dumps(chunks),
                    model='Qwen/Qwen3-Embedding-8B',corpus='catalog',catalog_sha256=digest)
            write(hashlib.sha256((ROOT/CATALOG).read_bytes()).hexdigest())
            reader=Retriever(None,path)
            self.assertEqual(reader.corpus,'catalog')
            self.assertTrue(reader.legacy_corpus)
            write('wrong')
            with self.assertRaisesRegex(ValueError,'knowledge_index_catalog_outdated_rebuild'):
                Retriever(None,path)

    def test_current_corpus_is_explicitly_classified_and_legacy_is_separate(self):
        current = documents(corpus="catalog")
        self.assertEqual(len({c['path'] for c in current}), 6)
        self.assertEqual({c['scope'] for c in current}, {'current_contract', 'verified_incident', 'technical_reference'})
        self.assertTrue(all(c['path'].startswith('diagnosis/knowledge/') for c in current))
        self.assertTrue(all(c['provenance']['sources'] and c['provenance']['revision'] for c in current))
        self.assertTrue(any(c['path'].startswith('docs/upstream/') for c in documents(corpus='legacy')))

    def test_catalog_rejects_paths_outside_knowledge_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'diagnosis/knowledge').mkdir(parents=True)
            entry = {'path': '../../secret.md', 'kind': 'current_contract', 'revision': 'v1',
                     'authority': 'test', 'applies_to': ['test'], 'sources': []}
            (root/'diagnosis/knowledge/catalog.json').write_text(json.dumps({
                'schema_version': 1, 'revision': 'v1', 'entries': [entry]}))
            with self.assertRaisesRegex(ValueError, 'invalid_knowledge_entry'):
                documents(root, corpus="catalog")

    async def test_supporting_implementation_changes_are_marked_separately(self):
        class Provider:
            async def embed(self, texts): return np.array([[1, 0]], dtype=np.float32)
            async def rerank(self, query, texts): return [0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = root/'note.md'; note.write_text('stable contract')
            code = root/'code.py'; code.write_text('original')
            catalog = root/'diagnosis/knowledge/catalog.json'
            catalog.parent.mkdir(parents=True); catalog.write_text('{"revision":"v1"}')
            import hashlib
            h = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            chunks = [{'id': 'one', 'path': 'note.md', 'text': 'stable contract', 'heading': 'contract',
                       'sha256': h(note), 'scope': 'current_contract',
                       'provenance': {'local_source_hashes': {'code.py': h(code)}, 'catalog_sha256': h(catalog)}}]
            index = root/'index.npz'
            np.savez(index, vectors=np.array([[1, 0]]), metadata=json.dumps(chunks),
                     model='Qwen/Qwen3-Embedding-8B', corpus='current')
            with patch('diagnosis.retrieval.ROOT', root):
                reader = Retriever(Provider(), index, legacy_corpus=True)
                self.assertFalse((await reader.search('contract'))[0]['supporting_source_changed'])
                code.write_text('modified implementation')
                value = (await reader.search('contract'))[0]
                self.assertFalse(value['source_changed'])
                self.assertTrue(value['supporting_source_changed'])
                self.assertFalse(value['catalog_changed'])
                catalog.write_text('{"revision":"v2"}')
                self.assertTrue((await reader.search('contract'))[0]['catalog_changed'])

    async def test_experiment_detail_is_uuid_only_and_preserves_history(self):
        seen = []
        def handle(request):
            seen.append(request.url.path)
            return httpx.Response(200, json={'data': {'report': {'resources': {'note': 'historical summary'}}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            tools = Tools(client, None)
            for value in ({'experiment_id': '../etc/passwd'}, {'experiment_id': 1}, {'experiment_id': 'x', 'url': 'http://other'}):
                with self.assertRaisesRegex(ValueError, 'invalid_experiment_id'):
                    await tools.call('get_experiment', value)
            key = '20000000-0000-0000-0000-000000000001'
            evidence = await tools.call('get_experiment', {'experiment_id': key})
            self.assertEqual(seen, ['/api/v1/experiments/'+key])
            self.assertEqual(evidence['scope'], 'historical_experiment_detail')
            self.assertIn('historical summary', evidence['excerpt'])
            self.assertIn('get_experiment', [v['function']['name'] for v in schemas()])

    def test_full_acceptance_rejects_missing_reports_before_starting_resources(self):
        directory = ROOT/'artifacts'
        before = set(directory.glob('stage5-live-*'))
        result = subprocess.run([sys.executable, '-B', str(ROOT/'testscript/test_stage5_live.py')],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('full acceptance requires --reports', result.stderr)
        self.assertEqual(before, set(directory.glob('stage5-live-*')))


if __name__ == '__main__':
    unittest.main()
