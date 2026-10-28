"""Corpus provenance, historical access, and production candidate isolation."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import httpx

from diagnosis.agent import Tools
from diagnosis.config import ROOT
from diagnosis.corpus import load_policy, markdown_headings, policy_digest
from diagnosis.evaluate import AblationRetriever
from diagnosis.retrieval import build, documents, Retriever


class CorpusTests(unittest.IsolatedAsyncioTestCase):
    def test_reviewed_real_corpus_preserves_contracts_cases_and_provenance(self):
        policy = load_policy()
        chunks = documents()
        self.assertEqual({c['path'] for c in chunks}, set(policy['sources']))
        self.assertEqual(len(policy['sources']), 25)
        for c in chunks:
            meta = c['corpus']
            self.assertTrue(meta['section_path'])
            self.assertTrue(meta['components'])
            self.assertTrue(meta['source_quality'])
            self.assertTrue(meta['applicable_version'])
            self.assertEqual(meta['policy_version'], policy['version'])
            self.assertEqual(c['sha256'], hashlib.sha256((ROOT/c['path']).read_bytes()).hexdigest())
        contract = next(c for c in chunks if c['heading'] == '读取和汇总语义')
        self.assertEqual(contract['corpus']['layer'], 'current_contract')
        self.assertEqual(contract['corpus']['superseded_by'], [])
        self.assertEqual(contract['corpus']['published_on'], '2026-10-24')
        obsolete = next(c for c in chunks if c['heading'] == '2026-10-26：最终报告输出协议更新')
        self.assertEqual(obsolete['corpus']['superseded_by'][0]['heading'], '2026-10-26：json_schema原生端点修正')
        topic = next(c for c in chunks if c['heading'] == '方案 B：逻辑过期（Logical Expire）')
        self.assertEqual(topic['corpus']['layer'], 'technical_principle')
        self.assertIsNone(topic['corpus']['published_on'])
        http = [c for c in chunks if c['path'] == 'docs/STAGE_2_HTTP_MONITOR.md']
        old_events = next(c for c in http if '未实现路径，含 events' in c['text'])
        self.assertTrue(old_events['corpus']['superseded_by'])
        self.assertNotIn('/api/v1/health', old_events['text'])
        self.assertTrue(any('/api/v1/health' in c['text'] and not c['corpus']['superseded_by'] for c in http))

    def test_primary_notes_distinguish_manual_facts_from_project_interpretation(self):
        chunks = documents()
        primary = [c for c in chunks if c['scope'] == 'primary_reference']
        self.assertEqual({c['path'] for c in primary}, {
            'docs/knowledge/LINUX_SIGNALS.md', 'docs/knowledge/LINUX_PROC_STATE.md',
            'docs/knowledge/LINUX_WAIT_STATE.md'})
        for c in primary:
            self.assertEqual(c['corpus']['layer'], 'technical_principle')
            self.assertEqual(c['corpus']['source_version'], 'Linux man-pages 6.17')
            self.assertEqual(len(c['corpus']['source_snapshot_sha256']), 64)
            self.assertIn('man7.org/', c['corpus']['source_url'])
            self.assertEqual(c['corpus']['summary_author'], 'project_assistant')
        local = next(c for c in chunks if c['heading'] == '本项目公开数据的边界')
        self.assertEqual(local['scope'], 'project_document')
        self.assertEqual(local['corpus']['source_quality'], 'project_interpretation')
        self.assertNotIn('docs/DIAGNOSIS_WORKER_STOP_CASE.md', {c['path'] for c in chunks})

    def test_source_edit_requires_review_before_build_or_paid_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for relative in load_policy()['sources']:
                target = root/relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT/relative, target)
            (root/'README.md').write_text('changed without reviewing annotations')
            with self.assertRaisesRegex(ValueError, 'corpus_source_changed_review_annotations'):
                documents(root)

    def test_code_block_comments_do_not_change_section_inheritance(self):
        raw = '# Contract\n## Build\n```bash\n# comment\n## not a section\n```\n### Real child\n'
        self.assertEqual(list(markdown_headings(raw)), [(1, 1, 'Contract'), (2, 2, 'Build'), (7, 3, 'Real child')])

    async def test_filter_precedes_both_recalls_and_keeps_useful_history_accessible(self):
        class Provider:
            calls = []
            async def embed(self, texts):
                return np.array([[1, 0]], dtype=np.float32)
            async def rerank(self, query, texts):
                self.calls.append(texts)
                return list(range(min(5, len(texts))))
        # More than 20 highly ranked proposals must not displace eligible candidates.
        chunks = [{'id': str(i), 'path': 'missing.md', 'sha256': 'old', 'heading': 'H',
                   'text': 'Agent 规划 ' * 10, 'scope': 'historical_reference',
                   'corpus': {'layer': 'planning_progress', 'superseded_by': []}} for i in range(25)]
        for layer, text in [('current_contract', 'Agent 已实现'), ('historical_case', 'Agent 故障案例'),
                            ('technical_principle', 'Agent 只读原理')]:
            chunks.append({**chunks[0], 'id': layer, 'text': text,
                           'corpus': {'layer': layer, 'superseded_by': []}})
        chunks.append({**chunks[0], 'id': 'superseded', 'text': 'Agent 旧协议',
                       'corpus': {'layer': 'historical_case', 'superseded_by': [{'path': 'new.md'}]}})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'index.npz'
            np.savez(path, vectors=np.array([[1, 0]]*len(chunks)), metadata=json.dumps(chunks),
                     model='Qwen/Qwen3-Embedding-8B', corpus_policy_sha256=policy_digest())
            provider = Provider()
            retriever = Retriever(provider, path)
            results = await retriever.search('Agent 当前实现')
            self.assertEqual({c['id'] for c in results}, {'current_contract', 'historical_case', 'technical_principle'})
            self.assertEqual(len(provider.calls[-1]), 3)
            self.assertTrue(all(c['retrieval_purpose'] == 'default' for c in results))
            historical = await retriever.search('Agent 历史规划')
            self.assertTrue(any(c['corpus']['layer'] == 'planning_progress' for c in historical))
            self.assertEqual(len(retriever.selection('Agent upstream history')[1]), len(chunks))
            for mode in ('bm25', 'dense', 'hybrid', 'hybrid_rerank'):
                values = await AblationRetriever(provider, path, mode).search('Agent 当前实现')
                self.assertEqual({c['id'] for c in values}, {c['id'] for c in results})
            self.assertEqual(await AblationRetriever(provider, path).search('Agent 当前实现'), results)

    async def test_legacy_index_is_explicit_and_offline_failure_is_atomic(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'index.npz'
            chunks = [{'id': 'old', 'heading': 'H', 'text': 'old', 'path': 'missing.md', 'sha256': 'old'}]
            np.savez(path, vectors=np.array([[1, 0]]), metadata=json.dumps(chunks), model='Qwen/Qwen3-Embedding-8B')
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'knowledge_index_policy_outdated_rebuild'):
                Retriever(None, path)
            self.assertEqual(len(Retriever(None, path, legacy_corpus=True).selection('旧版')[1]), 1)
            with patch('diagnosis.retrieval.documents', return_value=[{**chunks[0], 'text': 'new'}]), \
                    patch('diagnosis.retrieval.Providers') as provider:
                with self.assertRaisesRegex(ValueError, 'corpus_build_requires_embedding'):
                    await build(path, offline=True)
                provider.assert_not_called()
            self.assertEqual(path.read_bytes(), before)

    async def test_production_tools_preserve_layer_metadata_in_citable_evidence(self):
        chunk = next(c for c in documents() if c['heading'] == '读取和汇总语义')
        class Provider:
            async def embed(self, texts): return np.array([[1, 0]])
            async def rerank(self, query, texts): return [0]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'index.npz'
            np.savez(path, vectors=np.array([[1, 0]]), metadata=json.dumps([chunk]),
                     model='Qwen/Qwen3-Embedding-8B', corpus_policy_sha256=policy_digest())
            def forbidden(request): raise AssertionError('monitor must not be accessed')
            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
                tools = Tools(client, Retriever(Provider(), path))
                evidence = await tools.call('search_technical_docs', {'query': '指标完整能证明业务健康吗'})
            saved = json.loads(evidence[0]['excerpt'])
            self.assertEqual(saved['corpus'], chunk['corpus'])
            self.assertEqual(saved['retrieval_purpose'], 'default')
            self.assertFalse(saved['source_changed'])
            self.assertEqual(evidence[0]['id'], 'E1')


if __name__ == '__main__':
    unittest.main()
