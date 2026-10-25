"""Evaluation integrity: production-ranking parity, split isolation, offline calls and honest metrics."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import numpy as np

from diagnosis.providers import ProviderError
from diagnosis.retrieval import Retriever
from evaluation.diagnosis.fixtures import scenario
from evaluation.diagnosis.run import (SUITE, CachedProviders, RetrievalComparison,
    matches, retrieval_score, validate_suite, replay_transport)
from evaluation.diagnosis.report import summarize, markdown
from evaluation.diagnosis.run import digest, save


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_correction_replaces_only_changed_replay_and_rejects_product_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); parent = root/'parent'; child = root/'child'; suite = root/'suite.json'
            save(suite, {'cases': [{'id': 'D1', 'split': 'development'}]})
            before, after = {'D1': {'x': 1}}, {'D1': {'x': 2}}
            common = {'suite_sha256': digest(suite.read_bytes()), 'index_sha256': 'index',
                'prompt_sha256': 'prompt', 'models': {}, 'selection': {'split': 'all', 'ids': None},
                'input_kind': 'synthetic_contract_replay', 'diagnosis_enabled': True,
                'source_hashes': {'diagnosis/agent.py': 'same'}}
            save(parent/'manifest.json', {**common, 'run_id': 'old', 'fixture_sha256': digest(before)})
            save(child/'manifest.json', {**common, 'run_id': 'new', 'fixture_sha256': digest(after)})
            save(parent/'observable-fixtures.json', before); save(child/'observable-fixtures.json', after)
            save(parent/'retrieval/D1.json', {'case_id': 'D1', 'status': 'succeeded', 'methods': {}})
            for mode in ('rag', 'runtime_only'):
                save(parent/f'generation/D1-{mode}.json', {'case_id': 'D1', 'mode': mode, 'status': 'failed'})
                save(child/f'generation/D1-{mode}.json', {'case_id': 'D1', 'mode': mode, 'status': 'succeeded'})
            result = summarize(parent, suite, corrections=[child])
            self.assertEqual(result['generation']['all/rag']['succeeded'], 1)
            self.assertTrue(result['completion']['complete'])
            self.assertEqual(result['source_runs'], ['old', 'new'])
            manifest = json.loads((child/'manifest.json').read_text())
            manifest['source_hashes']['diagnosis/agent.py'] = 'changed'
            save(child/'manifest.json', manifest)
            with self.assertRaisesRegex(ValueError, 'production_code'):
                summarize(parent, suite, corrections=[child])

    def test_report_marks_partial_run_and_rejects_review_from_another_run(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            suite = root/'suite.json'
            save(suite, {'cases': [{'id': 'D1', 'split': 'development'}]})
            save(root/'manifest.json', {'suite_sha256': digest(suite.read_bytes()),
                'selection': {'split': 'all', 'ids': None}, 'run_id': 'one',
                'input_kind': 'synthetic_contract_replay', 'diagnosis_enabled': True})
            save(root/'retrieval/D1.json', {'case_id': 'D1', 'status': 'succeeded', 'methods': {
                'bm25': {'recall_at_5': None, 'reciprocal_rank': None, 'returned_despite_no_gold_answer': True}}})
            value = summarize(root, suite)
            self.assertFalse(value['completion']['complete'])
            self.assertIsNone(value['provisional_review']['semantic_accuracy'])
            self.assertIn('—', markdown(value))
            save(root/'reviews.json', {'run_id': 'another', 'reviews': []})
            with self.assertRaisesRegex(ValueError, 'different_run'):
                summarize(root, suite, root/'reviews.json')

    def test_suite_has_disjoint_families_and_complete_rubrics(self):
        suite = json.loads(SUITE.read_text())
        from diagnosis.retrieval import documents
        counts = validate_suite(suite, documents(corpus='legacy'))
        self.assertEqual(counts, {'cases': 24, 'development': 15, 'holdout': 9, 'families': 8})
        bad = json.loads(SUITE.read_text())
        bad['cases'][0]['split'] = 'holdout'
        with self.assertRaisesRegex(ValueError, 'family_leaks'):
            validate_suite(bad, documents(corpus='legacy'))

    def test_no_answer_does_not_inflate_or_deflate_recall(self):
        value = retrieval_score({'relevant': []}, {}, [{'path': 'anything', 'text': 'anything'}])
        self.assertIsNone(value['recall_at_5'])
        self.assertTrue(value['returned_despite_no_gold_answer'])

    def test_ranking_metrics_measure_evidence_units_and_first_hit(self):
        anchors = {'a': {'path': 'a', 'quote': 'first'}, 'b': {'path': 'b', 'quote': 'second'}}
        result = retrieval_score({'relevant': ['a', 'b']}, anchors,
            [{'path': 'other', 'text': 'first'}, {'path': 'a', 'text': 'first'}])
        self.assertEqual(result['recall_at_5'], .5)
        self.assertEqual(result['reciprocal_rank'], .5)

    async def test_offline_remote_cache_miss_cannot_make_paid_call(self):
        with tempfile.TemporaryDirectory() as root:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: self.fail('network called'))) as client:
                with self.assertRaisesRegex(ProviderError, 'offline_remote_call_forbidden'):
                    await CachedProviders(client, root, False, []).embed(['test'])

    async def test_ablation_rankings_match_actual_production_retrieval(self):
        class Provider:
            async def embed(self, texts): return np.array([[0, 1]], dtype=np.float32)
            async def rerank(self, query, texts): return list(reversed(range(len(texts))))
        with tempfile.TemporaryDirectory() as root:
            chunks = [{'id': str(i), 'path': 'missing.md', 'sha256': 'old',
                       'heading': 'H', 'text': text} for i, text in enumerate(['连接正常', '队列积压', '采样过期'])]
            path = Path(root)/'index.npz'
            np.savez(path, vectors=np.array([[1, 0], [0, 1], [-1, 0]]),
                     metadata=json.dumps(chunks), model='Qwen/Qwen3-Embedding-8B')
            production = Retriever(Provider(), path)
            expected = await production.search('队列积压')
            observed = await RetrievalComparison(production, Provider()).rankings('队列积压', True)
            self.assertEqual([production.chunks[i]['id'] for i in observed['hybrid_rerank']], [c['id'] for c in expected])

    async def test_replay_does_not_expose_labels_or_read_real_endpoints(self):
        replay = scenario('stale')
        self.assertNotIn('rubric', json.dumps(replay))
        self.assertIsNone(replay['replies']['workers']['data']['workers'][0]['metrics'])
        async with httpx.AsyncClient(transport=replay_transport(replay)) as client:
            self.assertEqual((await client.get('http://127.0.0.1/api/v1/workers')).json()['validity'], 'partial')
            self.assertEqual((await client.get('http://127.0.0.1/api/v1/run_shell')).status_code, 404)
        with self.assertRaisesRegex(ValueError, 'unknown_fixture'):
            scenario('typo')

    def test_event_replays_preserve_production_rule_value_shapes(self):
        for name in ('receive_backlog', 'send_backlog', 'queue_missing', 'active_not_current'):
            event = scenario(name)['replies']['events']['data']['events'][0]
            self.assertIsInstance(event['observed'], int)
            self.assertIsInstance(event['threshold'], int)
            self.assertEqual(set(event['worker']), {'pid', 'process_start_ticks', 'worker_index'})
        collection = scenario('stale')['replies']['events']['data']['events'][0]
        self.assertIsInstance(collection['observed'], dict)
        self.assertIsNone(collection['worker'])
        self.assertEqual(collection['threshold'], 'all_expected_workers_fresh')

    def test_experiment_summary_and_comparison_have_consistent_means(self):
        for name in ('different_load', 'missing_mean', 'zero_baseline'):
            replay = scenario(name)
            rows = replay['replies']['experiments']['data']['experiments']
            metric = replay['comparison']['data']['metrics']['mean_latency_ms']
            self.assertEqual(metric['baseline'], rows[0]['result']['latency_ms']['mean'])
            self.assertEqual(metric['candidate'], rows[1]['result']['latency_ms']['mean'])
        self.assertEqual(scenario('different_load')['comparison']['data']['metrics']['mean_latency_ms']['delta'], 1)
        self.assertIsNone(scenario('missing_mean')['comparison']['data']['metrics']['mean_latency_ms']['delta'])


if __name__ == '__main__':
    unittest.main()
