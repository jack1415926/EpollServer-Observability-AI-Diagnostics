"""Native Responses json_schema requests, preserving Agent evidence and failures."""
import json
import os
import unittest
from unittest.mock import patch
import httpx
from diagnosis.agent import Analysis
from diagnosis.providers import Providers

class ResponsesTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_schema_uses_responses_endpoint_and_retains_full_schema(self):
        def handler(request):
            self.assertEqual(str(request.url),'https://api.deepseek.com/responses')
            payload=json.loads(request.content)
            self.assertNotIn('response_format',payload)
            self.assertEqual(payload['text']['format']['type'],'json_schema')
            self.assertEqual(payload['text']['format']['schema'],Analysis.model_json_schema())
            self.assertEqual(payload['reasoning'],{'effort':'none'})
            return httpx.Response(200,json={'status':'completed','output':[{'type':'message','role':'assistant',
                'content':[{'type':'output_text','text':'{"ok":true}'}]}]})
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-key','DEEPSEEK_BASE_URL':'https://api.deepseek.com/beta'}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                text=await Providers(client).structured([{'role':'user','content':'JSON'}],Analysis.model_json_schema())
        self.assertEqual(json.loads(text),{'ok':True})

    async def test_native_tool_history_pairing_preserves_evidence(self):
        messages=[{'role':'system','content':'rules'}, {'role':'user','content':'question'},
                  {'role':'assistant','content':None,'tool_calls':[{'id':'c1','type':'function',
                   'function':{'name':'get_server_health','arguments':'{}'}}]},
                  {'role':'tool','tool_call_id':'c1','content':'{"evidence":"E1"}'},
                  {'role':'user','content':'final JSON'}]
        def handler(request):
            inputs=json.loads(request.content)['input']
            self.assertEqual(inputs[2],{'type':'function_call','call_id':'c1','name':'get_server_health','arguments':'{}'})
            self.assertEqual(inputs[3],{'type':'function_call_output','call_id':'c1','output':'{"evidence":"E1"}'})
            return httpx.Response(200,json={'status':'completed','output':[{'type':'reasoning','content':[]},
                {'type':'message','role':'assistant','content':[{'type':'output_text','text':'{"ok":'},
                                                             {'type':'output_text','text':'true}'}]}]})
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-key'}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                self.assertEqual(json.loads(await Providers(client).structured(messages,{})),{'ok':True})
                for bad in (messages[:3], [messages[3]], messages[:4]+[messages[3]]):
                    from diagnosis.providers import ProviderError
                    with self.assertRaisesRegex(ProviderError,'invalid_message_history'):
                        await Providers(client).structured(bad,{})

    async def test_refusal_failed_nonobject_and_unfinished_item_are_rejected(self):
        from diagnosis.providers import ProviderError
        for data in ('not an object',{'status':'failed','error':{'code':'failure'}},
                     {'status':'completed','output':[{'type':'message','role':'assistant','status':'incomplete','content':[]}]},
                     {'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'refusal','refusal':'no'}]}]}):
            with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-key'}):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=data))) as client:
                    with self.assertRaises(ProviderError):await Providers(client).structured([{'role':'user','content':'JSON'}],{})
