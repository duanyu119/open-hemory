"""Semantic state, evidence, correction and idempotency tests. All transcripts are fixtures."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mac'))
from semantic.store import SemanticStore, Conflict, source_rows
from semantic.conversation import build_conversations
from semantic.summarize import validate_summary, summarize
from semantic.providers import request_json, CloudBlocked


def fixture():
    cid=str(uuid.uuid4());session=str(uuid.uuid4())
    sources=[{'chunk_id':cid,'session_id':session,'sequence':0,'sha256':'a'*64,'started_at':'2026-09-19T08:00:00+08:00',
              'epoch':1789776000.0,'duration_seconds':300,'synthetic':False}]
    utterances=[{'id':f'u{i}','text':f'测试发言{i}','source_spans':[{'chunk_id':cid,'sha256':'a'*64,'start_ms':i*10000,'end_ms':i*10000+5000}]} for i in range(3)]
    analyses={cid:{'state':'succeeded','input_key':'k1','result':{'regions':[{'start_ms':0,'end_ms':30000,'kind':'speech','reason':'fixture'}],'utterances':utterances}}}
    return sources,analyses


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=SemanticStore(self.tmp.name,initialize=True)
        self.sources,self.analyses=fixture();self.doc=build_conversations(self.sources,self.analyses)[0]
        self.store.publish(self.doc,self.doc['input_key'])
    def tearDown(self):self.tmp.cleanup()
    def test_idempotent_publish_and_human_revision_protection(self):
        doc=self.store.detail(self.doc['id']);self.assertEqual(doc['revision'],1)
        self.assertFalse(self.store.publish(self.doc,self.doc['input_key']))
        response=self.store.correction(doc['id'],1,'rename',{'title':'人工标题'})
        self.assertEqual(response['revision'],2)
        changed=copy.deepcopy(self.doc);changed['title']='自动改动'
        self.assertFalse(self.store.publish(changed,'new-key'));self.assertEqual(self.store.detail(doc['id'])['title'],'人工标题')
        with self.assertRaises(Conflict):self.store.correction(doc['id'],1,'rename',{'title':'stale'})
        self.store.correction(doc['id'],2,'undo',{});self.assertEqual(self.store.detail(doc['id'])['title'],self.doc['title'])
    def test_split_merge_and_transactional_undo(self):
        cid=self.doc['id'];self.store.correction(cid,1,'split',{'at_utterance_id':'u1'})
        items=self.store.list()['items'];self.assertEqual(len(items),2)
        other=next(i for i in items if i['id']!=cid)
        self.assertEqual(len(self.store.detail(cid)['utterances']),1)
        self.store.correction(cid,2,'undo',{});self.assertEqual(len(self.store.list()['items']),1)
        self.assertEqual(len(self.store.detail(cid)['utterances']),3)
        self.assertFalse(self.store.detail(other['id'])['active'])
    def test_cross_chunk_and_gap_candidates(self):
        s=copy.deepcopy(self.sources[0]);s['chunk_id']=str(uuid.uuid4());s['sequence']=1;s['epoch']+=300
        u={'id':'next','text':'继续同一讨论','source_spans':[{'chunk_id':s['chunk_id'],'sha256':'a'*64,'start_ms':0,'end_ms':2000}]}
        # Put first chunk's last speech near its end so file boundary has no long silence.
        self.analyses[self.sources[0]['chunk_id']]['result']['utterances'][-1]['source_spans'][0].update(start_ms=297000,end_ms=300000)
        self.analyses[s['chunk_id']]={'state':'succeeded','input_key':'k2','result':{'utterances':[u],'regions':[]}}
        groups=build_conversations(self.sources+[s],self.analyses)
        # Earlier 4.5min actual silence is a candidate boundary, not the file boundary.
        containing=[g for g in groups if any(x['id']=='next' for x in g['utterances'])][0]
        self.assertEqual(len(containing['chunks']),2)
        s['sequence']=2
        separated=build_conversations(self.sources+[s],self.analyses)
        containing=[g for g in separated if any(x['id']=='next' for x in g['utterances'])][0]
        self.assertEqual(len(containing['chunks']),1)
    def test_silence_has_no_card_and_unknown_kept(self):
        cid=self.sources[0]['chunk_id'];self.analyses[cid]['result']={'regions':[{'start_ms':0,'end_ms':300000,'kind':'silence','reason':'fixture'}],'utterances':[]}
        self.assertEqual(build_conversations(self.sources,self.analyses),[])
        self.analyses[cid]['result']['regions'][0]['kind']='uncertain'
        self.assertEqual(build_conversations(self.sources,self.analyses)[0]['status'],'needs_review')
    def test_evidence_membership_and_missing_assignment(self):
        value={'title':'标题','overview':'概述','topics':[{'title':'主题','utterance_ids':['a'],'key_points':[]}],
               'key_points':[{'text':'事实','evidence_ids':['a']}]}
        result=validate_summary(value,['a','b']);self.assertEqual(result['topics'][-1]['utterance_ids'],['b'])
        value['key_points'][0]['evidence_ids']=['invented']
        with self.assertRaises(ValueError):validate_summary(value,['a','b'])
    def test_cloud_fails_closed_before_network(self):
        with patch('urllib.request.OpenerDirector.open') as network:
            with self.assertRaises(CloudBlocked):request_json(self.store,{'enabled':False},[],'test')
            self.assertFalse(network.called)
    def test_keyset_pages_do_not_duplicate(self):
        for i in range(6):
            doc=copy.deepcopy(self.doc);doc['id']=str(uuid.uuid4());doc['title']=f'测试{i}';self.store.publish(doc,str(i))
        a=self.store.list(limit=3);b=self.store.list(limit=3,cursor=a['next_cursor'])
        self.assertFalse({i['id'] for i in a['items']} & {i['id'] for i in b['items']})
        self.assertEqual(len(self.store.list(query='测试3')['items']),1)


if __name__=='__main__':unittest.main()
