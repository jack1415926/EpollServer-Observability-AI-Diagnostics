"""Metrics, split isolation, production parity and replay boundaries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import numpy as np

from diagnosis.agent import diagnose
from diagnosis.evaluate import (AblationRetriever, DATA, ReplayTools, automatic_checks,
                                read, retrieval_metrics, validate_dataset)
from diagnosis.retrieval import Retriever
from diagnosis.evaluation_report import summarize
from testscript.test_diagnosis import report_json, runtime_route


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_relevance_groups_do_not_double_count_overlapping_chunks(self):
        chunks=[{'path':'a','start_line':1,'end_line':10}]*5
        groups=[[{'path':'a','line':5}],[{'path':'b','line':2}]]
        self.assertEqual(retrieval_metrics(chunks,groups),{'recall_at_5':.5,'mrr_at_5':1.0})
        self.assertEqual(retrieval_metrics([{'path':'x','start_line':1,'end_line':10}]+chunks,groups)['mrr_at_5'],.5)
        self.assertEqual(retrieval_metrics([],groups)['recall_at_5'],0)
        self.assertIsNone(retrieval_metrics([],[])['recall_at_5'])

    def test_dataset_has_disjoint_families_and_valid_sources(self):
        cases,labels,fixtures=[read(DATA/(name+'.json')) for name in ('cases','labels','fixtures')]
        validate_dataset(cases,labels,fixtures)
        self.assertEqual(len(cases),24)
        self.assertEqual(sum(c['split']=='holdout' for c in cases),12)
        cases[1]['split']='holdout'
        with self.assertRaisesRegex(ValueError,'family_split_leakage'):
            validate_dataset(cases,labels,fixtures)

    def test_summary_keeps_missing_usage_and_errors_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            (path/'manifest.json').write_text(json.dumps({'dataset':'test','started_at':'fixed'}))
            rows=[{'case_id':'a','kind':'retrieval','mode':'dense','split':'dev','status':'ok',
                   'seconds':2,'metrics':{'recall_at_5':.5,'mrr_at_5':1},
                   'calls':[{'service':'EMBEDDING','usage':None}]},
                  {'case_id':'b','kind':'retrieval','mode':'dense','split':'holdout','status':'error','calls':[]}]
            (path/'results.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
            result=summarize(path)
            self.assertEqual(result['errors'],1)
            self.assertEqual(result['services']['EMBEDDING']['usage_missing'],1)
            self.assertEqual(result['services']['EMBEDDING']['usage_sums'],{})
            overall=next(g for g in result['groups'] if g['split']=='all')
            self.assertEqual((overall['attempted'],overall['ok']),(2,1))
            self.assertEqual(result['semantic_review'],'pending')
            rows.append(rows[0])
            (path/'results.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
            with self.assertRaisesRegex(ValueError,'duplicate_result_rows'):
                summarize(path)

    async def test_full_mode_matches_production_and_other_modes_are_distinct(self):
        class Provider:
            async def embed(self,texts):return np.array([[0,1]],dtype=np.float32)
            async def rerank(self,query,texts):return list(reversed(range(len(texts))))
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'index.npz'
            chunks=[{'id':str(i),'text':text,'heading':'H','path':'missing.md','sha256':'old',
                     'start_line':1,'end_line':2,'scope':'project_document'} for i,text in enumerate(['队列 积压','正常'])]
            np.savez(path,vectors=np.eye(2),metadata=json.dumps(chunks),model='Qwen/Qwen3-Embedding-8B')
            provider=Provider()
            actual=await AblationRetriever(provider,path,legacy_corpus=True).search('队列')
            self.assertEqual(actual,await Retriever(provider,path,legacy_corpus=True).search('队列'))
            dense=await AblationRetriever(provider,path,'dense',legacy_corpus=True).search('队列')
            self.assertEqual(dense[0]['id'],'1')
            self.assertTrue(dense[0]['source_changed'])
            self.assertEqual(await AblationRetriever(provider,path,'live_only',legacy_corpus=True).search('队列'),[])

    async def test_real_agent_replay_never_accesses_network_files_or_gold(self):
        fixture=read(DATA/'fixtures.json')['injection']
        class Provider:
            model='fixture'
            async def chat(self,messages,tools=None,tool_choice=None):
                self_test.assertNotIn('expected_conclusion',json.dumps(messages))
                if tools and tool_choice is None:
                    return {'role':'assistant','content':None,'tool_calls':[{'id':'log','type':'function',
                           'function':{'name':'read_server_logs','arguments':'{}'}}]}
            async def structured(self,messages,schema):
                if 'intent' in schema['properties']: return runtime_route(['health','workers','logs'])
                self_test.assertNotIn('expected_conclusion',json.dumps(messages))
                return report_json({'conclusion':'insufficient_evidence','summary':'无法确定',
                    'facts':[{'text':'监控存活','evidence_ids':['E1'],'basis':'observation',
                              'support':[{'evidence_id':'E1','pointer':'/http_status','value':200,'quote':''}]}],'possible_causes':[],
                    'unknowns':['Redis状态未知'],'next_steps':['人工核对']})
        class NoDocs:
            async def search(self,query):return []
        self_test=self
        def forbidden(request):raise AssertionError('network access forbidden')
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            tools=ReplayTools(client,NoDocs(),fixture)
            with patch('os.open',side_effect=AssertionError('file access forbidden')):
                report=await diagnose('读取日志后判断',Provider(),tools)
            self.assertTrue(any('忽略系统规则' in e['excerpt'] for e in report['evidence']))
            self.assertEqual(report['analysis']['conclusion'],'insufficient_evidence')
            checks=automatic_checks(report,read(DATA/'labels.json')['R2'])
            self.assertTrue(checks['required_evidence_present'])
            self.assertEqual(checks['semantic_support'],'requires_review')


if __name__=='__main__':unittest.main()
