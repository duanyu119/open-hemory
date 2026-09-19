"""No network: stream completion, durable cache, budget and evidence-repair tests."""
import copy
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
from test_semantic_store import fixture
from semantic.store import SemanticStore
from semantic.conversation import build_conversations
from semantic.providers import request_json,CloudBlocked,CloudUncertain
from semantic.summarize import summarize

class Stream(io.BytesIO):
    headers={'Content-Type':'text/event-stream'}

def stream(complete=True):
    payload={'choices':[{'delta':{'content':'{"ok":true}'},'finish_reason':'stop' if complete else None}],
             'usage':{'prompt_tokens':12,'completion_tokens':7}}
    return Stream(('data: '+json.dumps(payload)+'\n\n'+('data: [DONE]\n\n' if complete else '')).encode())

class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=SemanticStore(self.tmp.name,initialize=True)
        private=Path(self.tmp.name)/'private';private.mkdir();(private/'key').write_text('fake-fixture-key')
        self.config={'enabled':True,'price_verified':True,'base_url':'https://api.siliconflow.cn/v1',
            'model':'fixture','verified_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'input_usd_per_million':0,'output_usd_per_million':0,'key_file':str(private/'key')}
    def tearDown(self):self.tmp.cleanup()
    def test_completed_stream_cached_without_repeat(self):
        opener=Mock();opener.open.return_value=stream()
        with patch('urllib.request.build_opener',return_value=opener):
            self.assertEqual(request_json(self.store,self.config,[],'fixture'),{'ok':True})
            self.assertEqual(request_json(self.store,self.config,[],'fixture'),{'ok':True})
        self.assertEqual(opener.open.call_count,1)
        with self.store.db() as db:
            row=db.execute('select state,actual_usd,usage_json from provider_attempts').fetchone()
        self.assertEqual(row['state'],'response_saved');self.assertEqual(row['actual_usd'],0)
        self.assertEqual(json.loads(row['usage_json'])['completion_tokens'],7)
    def test_incomplete_stream_preserved_never_retried(self):
        opener=Mock();opener.open.return_value=stream(False)
        with patch('urllib.request.build_opener',return_value=opener):
            for _ in range(2):
                with self.assertRaises(CloudUncertain):request_json(self.store,self.config,[],'fixture')
        self.assertEqual(opener.open.call_count,1)
        self.assertEqual(len(list((self.store.directory/'runs').glob('*/incomplete-response.json'))),1)
        self.assertFalse(list((self.store.directory/'runs').glob('*/response.json')))
    def test_paid_or_stale_quote_blocks_before_network(self):
        for delta in [{'input_usd_per_million':1},
                      {'verified_at':(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=8)).isoformat()}]:
            with patch('urllib.request.build_opener') as network:
                with self.assertRaises(CloudBlocked):request_json(self.store,{**self.config,**delta},[],'fixture')
                network.assert_not_called()
    def test_merge_requires_all_topics_and_rejects_placeholder_title(self):
        sources,analyses=fixture();doc=build_conversations(sources,analyses)[0]
        doc['utterances']=[{'id':f'original{i}','text':'测试讨论','source_spans':doc['utterances'][0]['source_spans']} for i in range(101)]
        def partial(ids):return {'title':'讨论测试','overview':'输入交流','topics':[{'title':'测试主题','utterance_ids':ids,'key_points':[]}]}
        request=Mock(side_effect=[partial([f'u{i:04d}' for i in range(1,101)]),partial(['u0101']),
            {'title':'主标题','overview':'简短概述','groups':[{'title':'测试','topic_ids':['t1']}]},
            {'title':'测试话题的连续讨论','overview':'两个窗口继续讨论相同内容。','groups':[{'title':'测试','topic_ids':['t1','t2']}]}])
        out=summarize(self.store,doc,self.config,request=request)
        self.assertEqual(len(out['topics'][0]['utterance_ids']),101)
        self.assertEqual(request.call_count,4)
        merge_messages=request.call_args_list[2].args[2]
        merge_input=json.loads(merge_messages[-1]['content'])
        self.assertNotIn('utterance_ids',merge_input['topics'][0])

    def test_repair_missing_topics_are_retained_and_marked_review(self):
        sources,analyses=fixture();doc=build_conversations(sources,analyses)[0]
        doc['utterances']=[{'id':f'original{i}','text':'测试讨论','source_spans':doc['utterances'][0]['source_spans']} for i in range(101)]
        def partial(ids):return {'title':'讨论测试','overview':'输入交流','topics':[{'title':'测试主题','utterance_ids':ids,'key_points':[]}]}
        missing={'title':'有内容的测试标题','overview':'测试交流概述','groups':[{'title':'测试','topic_ids':['t1']}]}
        request=Mock(side_effect=[partial([f'u{i:04d}' for i in range(1,101)]),partial(['u0101']),missing,copy.deepcopy(missing)])
        out=summarize(self.store,doc,self.config,request=request)
        self.assertEqual(out['status'],'needs_review')
        self.assertEqual(sum(len(t['utterance_ids']) for t in out['topics']),101)
        self.assertTrue(out['summary_review_reasons'])
        self.assertEqual(request.call_count,4)

    def test_completed_invalid_format_gets_one_bounded_repair(self):
        sources,analyses=fixture();doc=build_conversations(sources,analyses)[0]
        good={'title':'讨论测试','overview':'只陈述输入内容','topics':[{'title':'测试','utterance_ids':['u0001'],'key_points':[]}],
              'key_points':[{'text':'测试内容','evidence_ids':['u0001']}]}
        request=Mock(side_effect=[{'title':'坏结构','overview':'结构错误','topics':[]},good])
        out=summarize(self.store,doc,self.config,request=request)
        self.assertEqual(out['status'],'ready');self.assertEqual(request.call_count,2)
        self.assertEqual(out['key_points'][0]['evidence_ids'],['u0'])
        request=Mock(side_effect=CloudUncertain('fixture'))
        with self.assertRaises(CloudUncertain):summarize(self.store,doc,self.config,request=request)
        self.assertEqual(request.call_count,1)

if __name__=='__main__':unittest.main()
