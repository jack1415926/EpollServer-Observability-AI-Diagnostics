"""Schema report generation, bounded repair and frozen anchor audit checks."""
import contextlib
import copy
import io
import json
import os
import unittest
from unittest.mock import patch
import httpx
from pydantic import ValidationError
from diagnosis.agent import diagnose, generate_analysis, schemas
from diagnosis.audit_retrieval import classify
from diagnosis.evaluate import DATA, ReplayTools, read
from diagnosis.providers import Providers, ProviderError

REPORT={'conclusion':'insufficient_evidence','summary':'证据不足',
        'facts':[{'text':'Redis指标未支持','evidence_ids':['E2'],'basis':'observation',
                  'support':[{'evidence_id':'E2','pointer':'/response/data/unsupported_metrics/4','value':'redis','quote':'','operation':'value'}]}],
        'possible_causes':[],'excluded_causes':[],'unknowns':['Redis实际状态未知'],'next_steps':['人工采集Redis证据']}


def response(report):
    return httpx.Response(200,json={'status':'completed','output':[{'type':'message','role':'assistant',
        'content':[{'type':'output_text','text':json.dumps(report)}]}]})


class NoDocs:
    async def search(self,query):return []


class StructuredTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_uses_responses_schema_after_tool_round(self):
        seen=[]
        def handler(request):
            payload=json.loads(request.content);seen.append(payload)
            if len(seen)==1:
                self.assertEqual(request.url.path,'/responses')
                return response({'intent':'runtime','runtime_sources':['health','workers'],
                                 'search_query':'','clarification':''})
            if len(seen)==2:
                self.assertEqual(request.url.path,'/beta/chat/completions')
                return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'ready'},'finish_reason':'stop'}]})
            self.assertEqual(request.url.path,'/responses')
            self.assertNotIn('tools',payload)
            schema=payload['text']['format']['schema']
            self.assertEqual(payload['text']['format']['type'],'json_schema')
            self.assertEqual(schema['$defs']['Claim']['properties']['evidence_ids']['items']['enum'],['E1','E2'])
            self.assertEqual(schema['properties']['unknowns']['minItems'],1)
            return response(REPORT)
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-key','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                report=await diagnose('Redis状态？',Providers(client),ReplayTools(client,NoDocs(),read(DATA/'fixtures.json')['complete']))
        self.assertEqual(report['analysis'],REPORT)
        self.assertEqual(report['output_validation']['requested_protocol'],'deepseek_responses_json_schema')
        self.assertEqual(len(seen),3)


class StructuredBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_tool_calls_use_beta_and_strict_without_duplicate_suffix(self):
        for base in ('https://api.deepseek.com','https://api.deepseek.com/v1','https://api.deepseek.com/beta'):
            def handler(request):
                self.assertEqual(str(request.url),'https://api.deepseek.com/beta/chat/completions')
                self.assertTrue(all(t['function']['strict'] for t in json.loads(request.content)['tools']))
                return httpx.Response(200,json={'choices':[{'message':{'content':'ready'},'finish_reason':'stop'}]})
            with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-key','DEEPSEEK_BASE_URL':base}):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    await Providers(client).chat([{'role':'user','content':'read health'}],schemas())

    async def test_backend_cardinality_repair_is_bounded_and_never_replays_tools(self):
        for recover in (True,False):
            calls=[]
            def handler(request):
                payload=json.loads(request.content);calls.append(payload)
                value=copy.deepcopy(REPORT)
                if not recover or len(calls)==1:value['unknowns']=[]
                if len(calls)>1:
                    correction=payload['input'][-1]
                    self.assertEqual(correction['role'],'user')
                    self.assertIn('unknowns',correction['content'])
                    self.assertIn('too_short',correction['content'])
                return response(value)
            with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-key'}):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    action=generate_analysis([{'role':'user','content':'JSON report'}],Providers(client),['E2'])
                    if recover:
                        report,repairs=await action
                        self.assertEqual(report.model_dump(),REPORT)
                        self.assertEqual(repairs,[[{'field':['unknowns'],'type':'too_short'}]])
                    else:
                        with self.assertRaises(ValidationError):await action
            self.assertEqual(len(calls),2)
            self.assertTrue(all('tools' not in p and p['text']['format']['type']=='json_schema' for p in calls))

    async def test_provider_schema_violation_is_recorded_and_not_silently_dropped(self):
        calls=[]
        def handler(request):
            calls.append(request)
            value=copy.deepcopy(REPORT)
            if len(calls)==1:value['conclusion_note']='unexpected'
            return response(value)
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-key'}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                report,repairs=await generate_analysis([{'role':'user','content':'JSON'}],Providers(client),['E2'])
        self.assertEqual(report.model_dump(),REPORT)
        self.assertEqual(repairs,[[{'field':['conclusion_note'],'type':'extra_forbidden'}]])
        self.assertEqual(len(calls),2)

    async def test_transport_failure_truncation_and_wrong_protocol_are_rejected(self):
        for scenario in ('unsupported','truncated','chat_envelope','empty'):
            calls=[]
            def handler(request):
                calls.append(request)
                if scenario=='unsupported':return httpx.Response(400,json={'error':'unsupported'})
                if scenario=='truncated':return httpx.Response(200,json={'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'}})
                if scenario=='chat_envelope':return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(REPORT)}}]})
                return httpx.Response(200,json={'status':'completed','output':[]})
            with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-key'}):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(ProviderError):
                        await generate_analysis([{'role':'user','content':'JSON'}],Providers(client),['E2'])
            self.assertEqual(len(calls),1)

    async def test_contract_probe_does_not_confuse_acceptance_with_enforcement(self):
        from diagnosis import probe
        def handler(request):
            self.assertEqual(str(request.url),'https://api.deepseek.com/beta/chat/completions')
            return httpx.Response(200,json={'choices':[{'finish_reason':'tool_calls','message':{
                'role':'assistant','tool_calls':[{'id':'probe','type':'function','function':{
                'name':'submit_probe','arguments':'{"ok":true,"extra_probe":null}'}}]}}]})
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-key','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with patch('diagnosis.probe.load_environment'),patch('diagnosis.probe.httpx.AsyncClient',return_value=client):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises(SystemExit) as failure:await probe.main(strict_contract=True)
            self.assertEqual(failure.exception.code,1)
            self.assertIn('deepseek_invalid_structured_response',output.getvalue())

    def test_anchor_audit_distinguishes_recall_fusion_and_rerank_loss(self):
        chunks={str(i):{'path':'a','start_line':i,'end_line':i} for i in range(1,5)}
        trace={'bm25':['1','2','3'],'dense':['4'],'candidates':['1','2'],'returned':['1']}
        self.assertEqual([classify([{'path':'a','line':i}],trace,chunks) for i in range(1,6)],
                         ['returned','rerank_dropped','fusion_dropped','fusion_dropped','not_in_union40'])
