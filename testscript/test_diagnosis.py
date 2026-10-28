"""Diagnostic trust boundaries, task lifecycle, citation checks and remote response contracts."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import numpy as np

from diagnosis.agent import Tools, diagnose
from diagnosis.config import redact
from diagnosis.providers import Providers, ProviderError
from diagnosis.retrieval import build, documents, Retriever, tokenize
from diagnosis.service import create_app

TOKEN = 'diagnosis-test-token-0123456789abcdef'


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.fail = False

    def initialize(self):
        for row in self.rows.values():
            if row['status'] in ('queued', 'running'):
                row.update(status='interrupted', error_code='service_restarted')

    def create(self, key, question):
        if self.fail:
            raise OSError()
        self.rows[key] = {'id': key, 'question': question, 'status': 'queued', 'report': None}

    def update(self, key, status, error=None, report=None):
        if self.fail:
            raise OSError()
        self.rows[key].update(status=status, error_code=error, report=report)

    def read(self, key=None):
        if self.fail:
            raise OSError()
        return copy.deepcopy(self.rows.get(key) if key else list(self.rows.values()))


async def eventually(check, seconds=3):
    async with asyncio.timeout(seconds):
        while not check():
            await asyncio.sleep(.01)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, {'EPOLL_DIAG_TOKEN': TOKEN, 'EPOLL_DIAG_TIMEOUT_SECONDS': '1'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = MemoryStore()

    async def open(self, runner):
        app = create_app(self.store, runner)
        life = app.router.lifespan_context(app)
        await life.__aenter__()
        self.addAsyncCleanup(life.__aexit__, None, None, None)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                  headers={'Authorization': 'Bearer '+TOKEN})
        self.addAsyncCleanup(client.aclose)
        await eventually(lambda: True)
        for _ in range(20):
            if (await client.get('/internal/health')).json()['data']['ready']:
                return client
            await asyncio.sleep(.01)
        self.fail('store not initialized')

    async def test_success_auth_validation_and_readback(self):
        async def run(question): return {'question': question, 'result': 'ok'}
        client = await self.open(run)
        self.assertEqual((await client.get('/internal/diagnoses', headers={'Authorization': 'bad'})).status_code, 401)
        for payload in ({'question': ' '}, {'question': 'ok', 'path': '/etc/passwd'}, {'question': 42}):
            self.assertEqual((await client.post('/internal/diagnoses', json=payload)).status_code, 400)
        self.assertEqual((await client.post('/internal/diagnoses', content='x'*4097)).status_code, 413)
        self.assertEqual((await client.get('/internal/diagnoses?limit=200')).status_code, 400)
        self.assertEqual((await client.get('/internal/diagnoses/invalid')).status_code, 400)
        accepted = await client.post('/internal/diagnoses', json={'question': '指标为何过期？'})
        self.assertEqual(accepted.status_code, 202)
        key = accepted.json()['data']['id']
        await eventually(lambda: self.store.rows[key]['status'] == 'succeeded')
        detail = (await client.get('/internal/diagnoses/'+key)).json()['data']
        self.assertEqual(detail['report']['result'], 'ok')

    async def test_real_deadline_cancels_runner_and_releases_capacity(self):
        cancelled = asyncio.Event()
        async def run(_):
            try: await asyncio.sleep(60)
            finally: cancelled.set()
        client = await self.open(run)
        key = (await client.post('/internal/diagnoses', json={'question': 'timeout'})).json()['data']['id']
        await eventually(lambda: self.store.rows[key]['status'] == 'timed_out')
        self.assertTrue(cancelled.is_set())
        self.assertEqual((await client.get('/internal/health')).json()['data']['outstanding'], 0)

    async def test_overload_and_shutdown_interrupt_running_work(self):
        async def run(_): await asyncio.sleep(60)
        client = await self.open(run)
        codes = [(await client.post('/internal/diagnoses', json={'question': 'wait'})).status_code for _ in range(11)]
        self.assertEqual(codes.count(202), 10)
        self.assertEqual(codes[-1], 503)

    async def test_store_failure_does_not_accept_or_call_model(self):
        async def run(_): self.fail('must not call model')
        client = await self.open(run)
        self.store.fail = True
        self.assertEqual((await client.post('/internal/diagnoses', json={'question': 'SQL failure'})).status_code, 503)
        self.assertEqual((await client.get('/internal/health')).status_code, 200)

    async def test_completed_result_waits_for_sql_recovery_without_reexecuting(self):
        calls = 0
        async def run(_):
            nonlocal calls
            calls += 1
            self.store.fail = True
            return {'result': 'actual-completion'}
        client = await self.open(run)
        key = (await client.post('/internal/diagnoses', json={'question': 'persist'})).json()['data']['id']
        await eventually(lambda: calls == 1)
        await eventually(lambda: self.store.rows[key]['status'] == 'running')
        self.assertEqual((await client.get('/internal/health')).json()['data']['outstanding'], 1)
        self.store.fail = False
        await eventually(lambda: self.store.rows[key]['status'] == 'succeeded')
        self.assertEqual(calls, 1)

    async def test_restart_marks_unfinished_work_and_keeps_completed_result(self):
        self.store.rows = {'old': {'status': 'running', 'report': None},
                           'done': {'status': 'succeeded', 'report': {'ok': True}}}
        async def run(_): self.fail('must not resume old jobs')
        await self.open(run)
        self.assertEqual(self.store.rows['old']['status'], 'interrupted')
        self.assertEqual(self.store.rows['done']['report'], {'ok': True})


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedding_restores_order_and_normalizes(self):
        def handler(request):
            return httpx.Response(200, json={'data': [{'index': 1, 'embedding': [0, 2]}, {'index': 0, 'embedding': [3, 0]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.dict(os.environ, {'EMBEDDING_API_KEY': 'test'}):
                vectors = await Providers(client).embed(['a', 'b'])
                np.testing.assert_allclose(vectors, np.eye(2))

    async def test_invalid_embedding_and_rerank_are_rejected(self):
        for name, payload in [('embedding', {'data': [{'index': 0, 'embedding': [0, 0]}]}),
                              ('rerank', {'results': [{'index': 3, 'relevance_score': .8}]})]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
                with patch.dict(os.environ, {'EMBEDDING_API_KEY': 'test', 'RERANK_API_KEY': 'test'}):
                    provider = Providers(client)
                    with self.assertRaises(ProviderError):
                        await (provider.embed(['a']) if name == 'embedding' else provider.rerank('q', ['a']))

    async def test_provider_failure_error_does_not_include_response_or_key(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(402, text='secret response'))) as client:
            with patch.dict(os.environ, {'EMBEDDING_API_KEY': 'private-test-key'}):
                with self.assertRaisesRegex(ProviderError, '^embedding_http_402$'):
                    await Providers(client).embed(['a'])


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebuild_reuses_identical_text_but_updates_provenance(self):
        calls = []
        class Provider:
            def __init__(self, client): pass
            async def embed(self, texts):
                calls.extend(texts)
                return np.array([[1, 0] for _ in texts], dtype=np.float32)
        chunks = [{'heading': 'H', 'text': 'same evidence', 'sha256': 'old'}]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'index.npz'
            with patch('diagnosis.retrieval.documents', return_value=chunks), patch('diagnosis.retrieval.Providers', Provider):
                await build(path)
                chunks[0]['sha256'] = 'new'
                await build(path)
            self.assertEqual(len(calls), 1)
            with np.load(path, allow_pickle=False) as saved:
                self.assertEqual(json.loads(str(saved['metadata']))[0]['sha256'], 'new')

    async def test_real_hybrid_fusion_rerank_and_source_change(self):
        class Provider:
            async def embed(self, texts): return np.array([[0, 1]], dtype=np.float32)
            async def rerank(self, query, texts): return [texts.index('队列 持续积压')]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'index.npz'
            chunks = [{'id': str(i), 'text': text, 'heading': 'H', 'path': 'missing.md',
                       'sha256': 'old', 'scope': 'project_document'} for i, text in enumerate(['正常', '队列 持续积压'])]
            np.savez(path, vectors=np.eye(2), metadata=json.dumps(chunks), model='Qwen/Qwen3-Embedding-8B')
            result = await Retriever(Provider(), path, legacy_corpus=True).search('队列积压')
            self.assertEqual(result[0]['id'], '1')
            self.assertTrue(result[0]['source_changed'])
        self.assertIn('队列', tokenize('Worker 队列积压'))

    def test_corpus_has_source_spans_hashes_and_historical_scope(self):
        chunks = documents()
        self.assertTrue(chunks)
        self.assertTrue(all(c['start_line'] <= c['end_line'] and len(c['sha256']) == 64 for c in chunks))
        self.assertTrue(any(c['scope'] == 'historical_reference' for c in chunks))
        self.assertTrue(any(c['scope'] == 'historical_reference' for c in documents(corpus='legacy')))


def report_json(report):
    return json.dumps(report)


def runtime_route(sources=None):
    return report_json({'intent':'runtime','runtime_sources':sources or ['health','workers'],
                        'search_query':'诊断指标契约','clarification':''})


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_more_than_four_requested_tools_receive_bounded_errors_then_valid_report(self):
        class Retriever:
            async def search(self, query): raise ProviderError('knowledge_index_missing')
        class Provider:
            model = 'test-model'
            rounds = 0
            async def chat(self, messages, tools=None, tool_choice=None):
                if tools and tool_choice is None:
                    self.rounds += 1
                    if self.rounds > 1: return {'content': 'ready'}
                    return {'role': 'assistant', 'content': None, 'tool_calls': [
                        {'id': 'call'+str(i), 'type': 'function', 'function': {
                            'name': 'get_server_health', 'arguments': '{}'}} for i in range(6)]}
            async def structured(self, messages, schema):
                if 'intent' in schema['properties']: return runtime_route()
                replies = [m for m in messages if m['role'] == 'tool']
                self_test.assertEqual(len(replies), 6)
                self_test.assertEqual(sum('invalid_or_exhausted' in m['content'] for m in replies), 2)
                return report_json({'conclusion': 'insufficient_evidence', 'summary': '未知',
                    'facts': [{'text': '采集不可用', 'evidence_ids': ['E1'], 'basis': 'observation',
                        'support': [{'evidence_id':'E1','pointer':'/http_status','value':503,'quote':''}]}], 'possible_causes': [],
                    'unknowns': ['根因未知'], 'next_steps': ['检查采集']})
        self_test = self
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503, json={'data': None}))) as client:
            report = await diagnose('why?', Provider(), Tools(client, Retriever()))
            self.assertEqual(report['analysis']['conclusion'], 'insufficient_evidence')

    async def test_citations_reject_fabrication_and_tools_cannot_read_chosen_path(self):
        class Retriever:
            async def search(self, query): raise ProviderError('embedding_http_402')
        class Provider:
            model = 'test-model'
            async def chat(self, messages, tools=None, tool_choice=None):
                return {'content': 'ready'}
            async def structured(self, messages, schema):
                if 'intent' in schema['properties']: return runtime_route()
                return report_json({'conclusion': 'insufficient_evidence', 'summary': '未知',
                    'facts': [{'text': 'unproven', 'evidence_ids': ['E999']}], 'possible_causes': [],
                    'unknowns': ['根因未知'], 'next_steps': ['检查采集']})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503, json={'data': None}))) as client:
            tools = Tools(client, Retriever())
            with self.assertRaisesRegex(ValueError, 'unexpected_tool_arguments'):
                await tools.call('read_server_logs', {'path': '/etc/passwd'})
            with self.assertRaisesRegex(ValueError, 'unknown_tool'):
                await tools.call('run_shell', {'command': 'echo test'})
            with self.assertRaisesRegex(ValueError, 'fabricated_evidence_reference'):
                await diagnose('why?', Provider(), tools)
            self.assertEqual(tools.evidence[-1]['scope'], 'unavailable')

    def test_redaction_covers_credentials_and_assignments(self):
        self.assertNotIn('secret-value', redact('password=secret-value'))
        self.assertNotIn('sk-abcdefghijk123', redact('key: sk-abcdefghijk123'))

    def test_structured_redaction_preserves_json_with_quotes_and_assignments(self):
        value = json.loads(redact({'question': 'password=hidden', 'nested': [{'token': 'private'}],
                                  'summary': 'api_key="quoted secret"'}))
        self.assertEqual(value['question'], 'password=[REDACTED]')
        self.assertEqual(value['nested'][0]['token'], '[REDACTED]')


if __name__ == '__main__':
    unittest.main()
