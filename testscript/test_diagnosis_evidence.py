"""Production evidence selection and report support trust boundaries."""
import json
import asyncio
import unittest
from unittest.mock import patch

import httpx

from diagnosis.agent import Tools, diagnose
from diagnosis.evaluate import ReplayTools, read
from diagnosis.retrieval import Retriever
from diagnosis.config import ROOT
from diagnosis.config import redact
from diagnosis.evidence import ReportSupportError, validate_report, numeric_supported
from diagnosis.evidence import Support
from diagnosis.agent import failure_report
from diagnosis.providers import Providers, ProviderError
from diagnosis.selection import contracts


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    def test_semantic_review_is_bound_to_reports_and_claim_counts(self):
        import tempfile,hashlib
        from pathlib import Path
        from diagnosis.review_evidence import report_hash,summarize
        report={'analysis':self.report()}
        row={'id':'T','strategy':'targeted','status':'ok','report':report,'calls':[],'tools':[],'seconds':.1}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);raw=json.dumps(row)+'\n';(root/'results.jsonl').write_text(raw)
            entry={'id':'T','strategy':'targeted','report_sha256':report_hash(report),'outcome':'pass',
                   'claim_support':{'facts':['supported'],'possible_causes':[],'excluded_causes':[]}}
            review={'results_sha256':hashlib.sha256(raw.encode()).hexdigest(),'scope':'fixture','reviewer':'fixture','reviews':[entry]}
            path=root/'review.json';path.write_text(json.dumps(review))
            self.assertEqual(summarize(root,path)['by_strategy']['targeted']['delivered'],1)
            entry['claim_support']['facts']=[];path.write_text(json.dumps(review))
            with self.assertRaisesRegex(ValueError,'review_claim_count_mismatch'):summarize(root,path)
            entry['report_sha256']='wrong';path.write_text(json.dumps(review))
            with self.assertRaisesRegex(ValueError,'review_report_hash_mismatch'):summarize(root,path)
    def test_redaction_preserves_serialized_evidence_and_is_idempotent(self):
        import hashlib
        tools=Tools(None,None)
        tools.record('technical_docs',{'text':'password=hidden'},'project_document')
        saved=json.loads(redact({'evidence':tools.evidence}))['evidence'][0]
        self.assertEqual(json.loads(saved['excerpt']),{'text':'password=[REDACTED]'})
        self.assertEqual(hashlib.sha256(saved['excerpt'].encode()).hexdigest(),saved['sha256'])
        original={'arguments':json.dumps({'password':'private phrase'}), 'text':'api_key="quoted phrase"'}
        first=json.loads(redact(original));second=json.loads(redact(first))
        self.assertEqual(first,second)
        self.assertEqual(json.loads(first['arguments'])['password'],'[REDACTED]')
        self.assertNotIn('quoted phrase',first['text'])
    def test_whitespace_quote_cannot_bypass_json_pointer_validation(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            Support(evidence_id='E1',pointer='/missing',value=None,quote=' ')
    def payload(self):
        tools = Tools(None, None)
        tools.record('workers', {'response': {'timestamp_unix_ms': 1793036067502, 'data': {'workers': [
            {'pid': 41001, 'process_start_ticks': 1100, 'worker_index': 0, 'status': 'fresh',
             'metrics': {'connections_current': 7, 'send_queue_messages': None}},
            {'pid': 41002, 'process_start_ticks': 1200, 'worker_index': 1, 'status': 'fresh',
             'metrics': {'connections_current': 9}}]}}}, 'live_observation')
        return tools.evidence

    def report(self, text='连接数7', pointer='/response/data/workers/0/metrics/connections_current', value=7):
        return {'summary': text, 'facts': [{'text': text, 'evidence_ids': ['E1'], 'basis': 'observation',
            'support': [{'evidence_id': 'E1', 'pointer': pointer, 'value': value, 'quote': ''}]}],
            'possible_causes': [], 'excluded_causes': []}

    def test_support_checks_numbers_times_identity_null_and_summary(self):
        evidence = self.payload()
        self.assertEqual(validate_report(self.report(), evidence)['checked_supports'], 1)
        bad = [self.report(value=999), self.report(value=True), self.report(text='连接数999'),
               self.report('PID41002', '/response/data/workers/0/pid', 41002),
               self.report('时间1793036067503', '/response/timestamp_unix_ms', 1793036067503),
               self.report('发送队列为0', '/response/data/workers/0/metrics/send_queue_messages', None)]
        summary = self.report(); summary['summary'] = '连接数999'; bad.append(summary)
        for report in bad:
            with self.subTest(report=report), self.assertRaises(ReportSupportError):
                validate_report(report, evidence)
        self.assertEqual(validate_report(self.report('发送队列未知，不是0',
            '/response/data/workers/0/metrics/send_queue_messages', None) | {'summary':'队列未知'}, evidence)['checked_supports'], 1)
        self.assertTrue(numeric_supported('1.23',['1.234']))
        self.assertTrue(numeric_supported('100000000000000000000',['1e+20']))
        self.assertFalse(numeric_supported('9'*400,['8'*400]))

    def test_exact_quotes_scope_and_worker_entity_binding(self):
        evidence = self.payload()
        report = self.report('连接数7和9')
        report['facts'][0]['support'].append({'evidence_id':'E1',
            'pointer':'/response/data/workers/1/metrics/connections_current','value':9,'quote':''})
        with self.assertRaises(ReportSupportError) as error:
            validate_report(report, evidence)
        self.assertIn('mixed_worker_entities', {e['type'] for e in error.exception.issues})
        tools = Tools(None, None)
        tools.record('user_statement', {'statement':'另一台机器很慢','verified':False}, 'user_statement')
        report = self.report('用户说明另一台机器很慢')
        claim = report['facts'][0]
        claim['support'] = [{'evidence_id':'E1','pointer':'','value':None,'quote':'另一台机器很慢'}]
        with self.assertRaises(ReportSupportError): validate_report(report, tools.evidence)
        claim['basis'] = 'user_statement'
        self.assertEqual(validate_report(report, tools.evidence)['checked_supports'],1)
        claim['support'][0]['quote'] = '原机器已死'
        with self.assertRaises(ReportSupportError): validate_report(report, tools.evidence)

    def test_empty_array_count_and_sibling_values_have_explicit_support(self):
        tools=Tools(None,None)
        tools.record('events',{'response':{'data':{'events':[]}}},'historical_records')
        report=self.report('历史事件列表为空','/response/data/events',0)
        report['facts'][0]['basis']='historical'
        report['facts'][0]['support'][0]['operation']='length'
        self.assertEqual(validate_report(report,tools.evidence)['checked_supports'],1)
        report['facts'][0]['support'][0]['value']=1
        with self.assertRaises(ReportSupportError): validate_report(report,tools.evidence)
        report=self.report('Worker0进程PID41001', '/response/data/workers/0/status','fresh')
        self.assertEqual(validate_report(report,self.payload())['checked_supports'],1)

    def test_full_instance_identifier_is_checked_as_a_string(self):
        tools=Tools(None,None)
        old='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        tools.record('events',{'instance':old},'historical_records')
        report=self.report('历史实例'+old,'/instance',old)
        report['facts'][0]['basis']='historical'
        validate_report(report,tools.evidence)
        report['facts'][0]['text']='历史实例bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        with self.assertRaises(ReportSupportError):validate_report(report,tools.evidence)

    def test_control_plane_counts_are_observations_but_event_contents_are_historical(self):
        tools=Tools(None,None)
        tools.record('events',{'response':{'data':{'events':[{'status':'active'}],
            'detection':{'instance':'a'*64}}}},'historical_records')
        report=self.report('查询返回1条记录','/response/data/events',1)
        report['facts'][0]['support'][0]['operation']='length'
        validate_report(report,tools.evidence)
        report=self.report('事件仍active','/response/data/events/0/status','active')
        with self.assertRaises(ReportSupportError):validate_report(report,tools.evidence)
        report=self.report('当前实例'+'a'*64,'/response/data/detection/instance','a'*64)
        validate_report(report,tools.evidence)

    def test_exclusion_can_combine_contract_and_observation_and_source_stage_is_not_a_metric(self):
        evidence=self.payload();tools=Tools(None,None);tools.evidence=evidence
        doc=tools.record('technical_docs',{'path':'docs/STAGE_1_COMPLETION.md','text':'名单不证明存活'},'project_document')
        claim={'text':'阶段1说明名单不证明存活','evidence_ids':['E2'],'basis':'document',
               'support':[{'evidence_id':'E2','pointer':'','quote':'名单不证明存活','value':None}]}
        report={'summary':'名单不是存活证明','facts':[],'possible_causes':[],'excluded_causes':[claim]}
        claim['evidence_ids'].append('E1')
        claim['support'].append({'evidence_id':'E1','pointer':'/response/data/workers/0/status','value':'fresh','quote':''})
        validate_report(report,tools.evidence)
        claim['text']='阶段999说明名单不证明存活'
        with self.assertRaises(ReportSupportError):validate_report(report,tools.evidence)

    def test_manual_section_number_is_a_verified_source_label(self):
        tools=Tools(None,None)
        tools.record('technical_docs',{'text':'来源signal(7)。不是运行观测。'},'project_document')
        report=self.report('signal(7)不是运行观测')
        report['facts'][0].update(basis='document',support=[{
            'evidence_id':'E1','pointer':'','value':None,'quote':'不是运行观测。'}])
        validate_report(report,tools.evidence)
        report['facts'][0]['text']='signal(99)不是运行观测'
        with self.assertRaises(ReportSupportError):validate_report(report,tools.evidence)

    def test_known_contradictions_are_checked_outside_facts_too(self):
        evidence = self.payload()
        value = json.loads(evidence[0]['excerpt']); value['response']['data']['workers'][0]['status'] = 'dead'
        tools = Tools(None, None); tools.record('workers', value, 'live_observation')
        for summary in ('共享内存快照停止刷新', 'SIGSTOP暂停导致dead', 'Master会自动补起Worker'):
            report = self.report(); report['summary'] = summary
            with self.subTest(summary=summary), self.assertRaises(ReportSupportError):
                validate_report(report, tools.evidence)
        report = self.report(); report['summary'] = '单纯SIGSTOP暂停不会导致dead；项目不自动补起。'
        validate_report(report, tools.evidence)
        value['response']['data']['workers'][0]['status']='process_unavailable'
        tools=Tools(None,None);tools.record('workers',value,'live_observation')
        report=self.report('进程状态未知','/response/data/workers/0/status','process_unavailable')
        report['possible_causes']=[{**report['facts'][0],'text':'SIGSTOP暂停导致process_unavailable'}]
        with self.assertRaises(ReportSupportError):validate_report(report,tools.evidence)
        tools=Tools(None,None);tools.record('workers',{'http_status':503},'live_observation')
        report=self.report('HTTP503','/http_status',503);report['summary']='overview返回HTTP503'
        with self.assertRaises(ReportSupportError):validate_report(report,tools.evidence)

    def test_multiline_quote_is_exact_and_changed_source_becomes_historical(self):
        tools = Tools(None, None)
        item = tools.record('technical_docs', {'text':'第一行\n第二行','source_changed':True}, 'project_document')
        self.assertEqual(item['basis'], 'historical')
        report = self.report('历史资料有两行')
        report['facts'][0].update(basis='historical', support=[{
            'evidence_id':'E1','pointer':'','value':None,'quote':'第一行\n第二行'}])
        validate_report(report, tools.evidence)
        report['facts'][0]['support'][0]['quote'] = '第一行第二行'
        with self.assertRaises(ReportSupportError): validate_report(report, tools.evidence)

    def test_contracts_are_current_versioned_and_source_change_fails_closed(self):
        values = contracts(['worker_states','worker_policy'])
        self.assertIn('不自动补起', values[1]['text'])
        for value in values:
            self.assertEqual(len(value['sha256']),64)
            self.assertTrue(value['references'])
        from diagnosis import selection
        with patch.object(selection, 'REGISTRY', ROOT/'missing-contract.json'):
            evidence = Tools(None,None).read_contracts(['worker_states'])
        self.assertEqual(evidence[0]['scope'],'unavailable')
        tools = Tools(None,None)
        first=tools.read_contracts(['worker_states','worker_policy'])
        self.assertEqual(tools.read_contracts(['worker_policy']),[first[1]])
        self.assertEqual(len(tools.evidence),2)

    async def test_provider_records_safe_usage_and_failures_and_retrievers_isolate_tasks(self):
        import os
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401,text='private-response'))) as client:
            with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'private-api-key'}):
                provider=Providers(client)
                with self.assertRaises(ProviderError):
                    await provider.structured([{'role':'user','content':'JSON'}],{})
        telemetry=json.dumps(provider.telemetry)
        self.assertNotIn('private-response',telemetry)
        self.assertNotIn('private-api-key',telemetry)
        self.assertEqual(provider.telemetry[0]['error'],'deepseek_http_401')
        self.assertIsNone(provider.telemetry[0]['usage'])
        original=Retriever(None)
        first,second=original.for_task(object()),original.for_task(object())
        first.traces.append({'query':'first'})
        self.assertEqual(second.traces,[]);self.assertEqual(original.traces,[])
        self.assertIs(first.vectors,second.vectors)
        failed=failure_report('问题','report_support_invalid',provider,Tools(None,first))
        self.assertEqual(failed['delivery_status'],'rejected')
        self.assertEqual(failed['analysis']['facts'],[])
        self.assertEqual(failed['reproducibility']['retrieval'],[{'query':'first'}])

    async def test_service_persists_rejected_report_metadata_without_success_claims(self):
        import os
        from diagnosis.service import create_app
        from testscript.test_diagnosis import MemoryStore, eventually, TOKEN
        fixture = read(ROOT/'evaluation/diagnosis_v1/fixtures.json')['complete']
        def response(value):
            return httpx.Response(200,json={'model':'returned-test-model','status':'completed',
                'usage':{'total_tokens':12},'output':[{'type':'message','role':'assistant',
                    'content':[{'type':'output_text','text':json.dumps(value)}]}]})
        def transport(request):
            if request.url.path=='/api/v1/workers': return httpx.Response(200,json=fixture['workers']['response'])
            if request.url.path.endswith('/chat/completions'):
                return httpx.Response(200,json={'model':'returned-test-model','usage':{'total_tokens':3},
                    'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'ready'}}]})
            payload=json.loads(request.content)
            if 'intent' in payload['text']['format']['schema']['properties']:
                return response({'intent':'runtime','runtime_sources':['workers'],'search_query':'','clarification':''})
            value={'conclusion':'insufficient_evidence','summary':'连接数999',
                'facts':[{'text':'连接数999','evidence_ids':['E1'],'basis':'observation','support':[
                    {'evidence_id':'E1','pointer':'/response/data/workers/0/metrics/connections_current','value':999,'quote':''}]}],
                'possible_causes':[],'unknowns':['未知'],'next_steps':['人工核对']}
            return response({k:v for k,v in value.items() if k in payload['text']['format']['schema']['properties']})
        store=MemoryStore()
        with patch.dict(os.environ,{'EPOLL_DIAG_TOKEN':TOKEN,'DEEPSEEK_API_KEY':'private-test-key',
                                   'DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            app=create_app(store)
            network=httpx.AsyncClient(transport=httpx.MockTransport(transport))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                    headers={'Authorization':'Bearer '+TOKEN}) as api:
                with patch('diagnosis.service.httpx.AsyncClient',return_value=network):
                    async with app.router.lifespan_context(app):
                        for _ in range(50):
                            if (await api.get('/internal/health')).json()['data']['ready']:break
                            await asyncio.sleep(.01)
                        accepted=await api.post('/internal/diagnoses',json={'question':'当前Worker连接数多少？'})
                        self.assertEqual(accepted.status_code,202)
                        key=accepted.json()['data']['id']
                        await eventually(lambda: store.rows[key]['status']=='failed')
                        row=(await api.get('/internal/diagnoses/'+key)).json()['data']
                        self.assertEqual(row['error_code'],'report_support_invalid')
                        self.assertEqual(row['report']['delivery_status'],'rejected')
                        self.assertEqual(row['report']['analysis']['facts'],[])
                        self.assertEqual(len(row['report']['reproducibility']['model_calls']),3)
                        self.assertNotIn('private-test-key',json.dumps(row))

    async def test_runtime_status_explanation_reads_contracts_when_router_omits_search(self):
        class Provider:
            model = 'fixture'
            async def structured(self, messages, schema):
                if 'intent' in schema['properties']:
                    return json.dumps({'intent': 'runtime', 'runtime_sources': ['workers'],
                                       'search_query': '', 'clarification': ''})
                payload = '\n'.join(m.get('content') or '' for m in messages)
                self_test.assertIn('process_unavailable', payload)
                self_test.assertIn('不自动补起', payload, 'selected Worker status needs the current project contract')
                return json.dumps({'conclusion': 'insufficient_evidence', 'summary': '不可读不证明退出',
                    'facts': [], 'possible_causes': [], 'unknowns': ['根因未知'], 'next_steps': ['人工核验']})
            async def chat(self, *args, **kwargs): return {'content': 'ready'}
        def forbidden(request): raise AssertionError('fixed replay must not access live HTTP')
        self_test = self
        fixture = read(ROOT/'evaluation/knowledge_t5/fixtures.json')['unreadable']
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            tools = ReplayTools(client, Retriever(None), fixture)
            await diagnose('请核对process_unavailable，是否已经退出并会自动重启？', Provider(), tools)

    async def test_final_report_checks_typed_values_not_just_existing_ids(self):
        class Provider:
            model = 'fixture'; finals = 0
            async def structured(self, messages, schema):
                if 'intent' in schema['properties']:
                    return json.dumps({'intent': 'runtime', 'runtime_sources': ['workers'],
                                       'search_query': '', 'clarification': ''})
                self.finals += 1
                value={'conclusion': 'insufficient_evidence', 'summary': '连接数为999',
                    'facts': [{'text': '当前连接数999', 'evidence_ids': ['E1'], 'basis': 'observation',
                        'support': [{'evidence_id': 'E1', 'pointer': '/response/data/workers/0/metrics/connections_current',
                                     'value': 999, 'quote': ''}]}],
                    'possible_causes': [], 'unknowns': ['根因未知'], 'next_steps': ['人工核验']}
                return json.dumps({k:v for k,v in value.items() if k in schema['properties']})
            async def chat(self, *args, **kwargs): return {'content': 'ready'}
        fixture = read(ROOT/'evaluation/diagnosis_v1/fixtures.json')['complete']
        provider = Provider()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
            with self.assertRaisesRegex(ValueError, 'report_support_invalid'):
                await diagnose('当前Worker连接数是多少？', provider, ReplayTools(client, None, fixture))
        self.assertEqual(provider.finals, 2, 'support repair must share the one final-report repair budget')


if __name__ == '__main__':
    unittest.main()
