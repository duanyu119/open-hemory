"""Worker integration on fake local media and stub summaries; no paid calls."""
import contextlib
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mac'))
from semantic.store import SemanticStore
import semantic_worker as worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=SemanticStore(self.root,initialize=True)
        self.cid=str(uuid.uuid4());self.sha=hashlib.sha256(b'fake-test-only').hexdigest()
        path=self.root/'chunks'/self.cid/(self.cid+'.m4a');path.parent.mkdir(parents=True);path.write_bytes(b'fake-test-only')
        meta={'chunk_id':self.cid,'session_id':str(uuid.uuid4()),'sequence':0,'started_at':'2026-09-19T12:00:00+08:00','sha256':self.sha}
        self.meta=meta
        with contextlib.closing(sqlite3.connect(self.root/'queue.sqlite3')) as db, db:
            db.execute('CREATE TABLE chunks(chunk_id TEXT,metadata TEXT,duration REAL,status TEXT)')
            db.execute('INSERT INTO chunks VALUES(?,?,?,?)',(self.cid,json.dumps(meta),10,'done'))
        self.version=1;self.calls=0
        self.config={'local':{},'cloud':{'enabled':True,'model':'test-only'}}
    def tearDown(self):self.temp.cleanup()
    def analyzer(self,path,source,local):
        return {'duration_ms':10000,'regions':[{'start_ms':0,'end_ms':10000,'kind':'speech'}],
                'utterances':[{'id':'test-u1' if source['chunk_id']==self.cid else source['chunk_id']+'-u',
                    'text':f'测试发言{self.version}',
                    'source_spans':[{'chunk_id':source['chunk_id'],'sha256':source['sha256'],'start_ms':0,'end_ms':10000}]}]}
    def add_chunk(self,sequence):
        cid=str(uuid.uuid4())
        path=self.root/'chunks'/cid/(cid+'.m4a');path.parent.mkdir(parents=True);path.write_bytes(b'fake-test-only')
        meta={**self.meta,'chunk_id':cid,'sequence':sequence,
              'started_at':(dt.datetime.fromisoformat(self.meta['started_at'])+dt.timedelta(seconds=10*sequence)).isoformat()}
        with contextlib.closing(sqlite3.connect(self.root/'queue.sqlite3')) as db, db:
            db.execute('INSERT INTO chunks VALUES(?,?,?,?)',(cid,json.dumps(meta),10,'done'))
        return cid
    def summary(self,store,doc,config):
        self.calls+=1;out=copy.deepcopy(doc);out.update(title='测试摘要',status='ready');return out
    def test_idempotency_and_local_only_control_survives_recompute(self):
        with patch.object(worker,'summarize',self.summary):
            worker.process(self.store,self.config,analyzer=self.analyzer)
            worker.process(self.store,self.config,analyzer=self.analyzer)
            self.assertEqual(self.calls,1)
            with self.store.db(True) as db:
                db.execute("UPDATE analyses SET state='pending'")
                db.execute('INSERT INTO processing_controls VALUES(?,?,?)',(self.cid,1,'用户要求仅本地重算'))
            self.version=2
            worker.process(self.store,self.config,analyzer=self.analyzer)
            self.assertEqual(self.calls,1)
            detail=self.store.detail(self.store.list()['items'][0]['id'])
            self.assertEqual(detail['utterances'][0]['text'],'测试发言2')
    def test_ready_summary_and_source_key_survive_repeated_cycles(self):
        with patch.object(worker,'summarize',self.summary):
            worker.process(self.store,self.config,analyzer=self.analyzer)
            before=self.store.detail(self.store.list()['items'][0]['id'])
            for _ in range(4):
                worker.process(self.store,self.config,analyzer=self.analyzer)
            after=self.store.detail(before['id'])
        self.assertEqual(after,before)
        self.assertEqual(after['status'],'ready')
        self.assertEqual(self.calls,1)
        self.assertNotEqual(after['summary_key'],after['input_key'])
        with self.store.db() as db:
            row=db.execute('SELECT input_key FROM conversations WHERE id=?',(after['id'],)).fetchone()
        self.assertEqual(row['input_key'],after['input_key'])

    def test_legacy_summary_index_key_is_normalized_without_new_revision_or_call(self):
        with patch.object(worker,'summarize',self.summary):
            worker.process(self.store,self.config,analyzer=self.analyzer)
            before=self.store.detail(self.store.list()['items'][0]['id'])
            with self.store.db(True) as db:
                db.execute('UPDATE conversations SET input_key=? WHERE id=?',(before['summary_key'],before['id']))
            worker.process(self.store,self.config,analyzer=self.analyzer)
        self.assertEqual(self.store.detail(before['id']),before)
        self.assertEqual(self.calls,1)
        with self.store.db() as db:
            row=db.execute('SELECT input_key FROM conversations WHERE id=?',(before['id'],)).fetchone()
        self.assertEqual(row['input_key'],before['input_key'])

    def test_source_change_summarizes_once_then_stays_ready(self):
        with patch.object(worker,'summarize',self.summary):
            worker.process(self.store,self.config,analyzer=self.analyzer)
            self.version=2
            with self.store.db(True) as db:db.execute("UPDATE analyses SET state='pending'")
            worker.process(self.store,self.config,analyzer=self.analyzer)
            before=self.store.detail(self.store.list()['items'][0]['id'])
            for _ in range(3):worker.process(self.store,self.config,analyzer=self.analyzer)
        self.assertEqual(self.calls,2)
        self.assertEqual(self.store.detail(before['id']),before)
        self.assertEqual(before['status'],'ready')
        self.assertEqual(before['utterances'][0]['text'],'测试发言2')

    def test_manual_revision_survives_changed_ids_and_late_upload(self):
        worker.process(self.store,self.config,no_cloud=True,analyzer=self.analyzer)
        original=self.store.detail(self.store.list()['items'][0]['id'])
        self.store.correction(original['id'],original['revision'],'edit_utterance',
                              {'utterance_id':'test-u1','text':'人工校订文字'})
        manual=self.store.detail(original['id'])
        late=self.add_chunk(1)
        with self.store.db(True) as db:db.execute("UPDATE analyses SET state='pending'")
        def changed_ids(path,source,local):
            result=self.analyzer(path,source,local)
            result['utterances'][0]['id']+='-new'
            return result
        for _ in range(3):worker.process(self.store,self.config,no_cloud=True,analyzer=changed_ids)
        self.assertEqual(self.store.detail(original['id']),manual)
        items=self.store.list()['items']
        self.assertEqual(len(items),2)
        supplement=next(self.store.detail(i['id']) for i in items if i['id']!=original['id'])
        self.assertEqual({s['chunk_id'] for u in supplement['utterances'] for s in u['source_spans']},{late})

    def test_late_unrecognized_audio_keeps_a_separate_review_card(self):
        worker.process(self.store,self.config,no_cloud=True,analyzer=self.analyzer)
        original=self.store.detail(self.store.list()['items'][0]['id'])
        self.store.correction(original['id'],original['revision'],'rename',{'title':'人工原对话'})
        manual=self.store.detail(original['id'])
        late=self.add_chunk(1)
        def uncertain(path,source,local):
            result=self.analyzer(path,source,local)
            if source['chunk_id']==late:
                result['utterances']=[]
                result['regions'][0]['kind']='uncertain'
            return result
        with patch.object(worker,'summarize',self.summary):
            for _ in range(3):worker.process(self.store,self.config,analyzer=uncertain)
        self.assertEqual(self.store.detail(original['id']),manual)
        self.assertEqual(self.calls,0)
        items=self.store.list()['items']
        self.assertEqual(len(items),2)
        supplement=next(self.store.detail(i['id']) for i in items if i['id']!=original['id'])
        self.assertEqual(supplement['utterances'],[])
        self.assertEqual(supplement['status'],'needs_review')
        self.assertEqual([c['chunk_id'] for c in supplement['chunks']],[late])
        self.assertEqual(supplement['duration_seconds'],10)

    def test_retry_status_change_without_words_updates_projection(self):
        def uncertain(path,source,local):
            result=self.analyzer(path,source,local)
            result['utterances']=[]
            result['regions'][0]['kind']='uncertain'
            return result
        worker.process(self.store,self.config,no_cloud=True,analyzer=uncertain)
        first=self.store.detail(self.store.list()['items'][0]['id'])
        with self.store.db(True) as db:db.execute("UPDATE analyses SET state='pending'")
        def failed(*args):raise ValueError('fixture failure')
        worker.process(self.store,self.config,no_cloud=True,analyzer=failed)
        second=self.store.detail(first['id'])
        self.assertNotEqual(first['input_key'],second['input_key'])
        self.assertGreater(second['revision'],first['revision'])
        self.assertEqual(second['regions'],[])

    def test_concurrent_manual_edit_wins_summary_publication(self):
        def edit_during_summary(store,doc,config):
            current=store.detail(doc['id'])
            store.correction(doc['id'],current['revision'],'rename',{'title':'人工标题'})
            return self.summary(store,doc,config)
        with patch.object(worker,'summarize',edit_during_summary):
            worker.process(self.store,self.config,analyzer=self.analyzer)
        detail=self.store.detail(self.store.list()['items'][0]['id'])
        self.assertTrue(detail['manual'])
        self.assertEqual(detail['title'],'人工标题')
        self.assertFalse((self.store.directory/'exports'/(detail['id']+'.md')).exists())

    def test_bad_summary_output_does_not_stop_worker(self):
        # Two independent conversations: the first summary raises the exact
        # AttributeError that a non-object topic used to produce; the worker must
        # record it as needs_review and still process the second conversation.
        self.add_chunk(2)
        calls={'n':0}
        def bad_first(store,doc,config):
            calls['n']+=1
            if calls['n']==1:
                raise AttributeError("'int' object has no attribute 'get'")
            out=copy.deepcopy(doc);out.update(title='正常摘要',status='ready');return out
        with patch.object(worker,'summarize',bad_first):
            worker.process(self.store,self.config,analyzer=self.analyzer)
        self.assertEqual(calls['n'],2)
        items=self.store.list()['items']
        self.assertEqual(len(items),2)
        # The bad task's job is needs_review; the good task's job succeeded. The
        # conversation itself stays transcribed (summary never published).
        statuses={self.store.detail(i['id'])['status'] for i in items}
        self.assertIn('transcribed',statuses)
        self.assertIn('ready',statuses)
        with self.store.db() as db:
            job_states=[r['state'] for r in db.execute('SELECT state FROM jobs')]
        self.assertIn('needs_review',job_states)
        self.assertIn('succeeded',job_states)

    def test_multiple_undo_restores_history(self):
        worker.process(self.store,self.config,no_cloud=True,analyzer=self.analyzer)
        doc=self.store.detail(self.store.list()['items'][0]['id']);cid=doc['id']
        a=self.store.correction(cid,doc['revision'],'rename',{'title':'A'})
        b=self.store.correction(cid,a['revision'],'rename',{'title':'B'})
        undo_b=self.store.correction(cid,b['revision'],'undo',{})
        self.assertEqual(self.store.detail(cid)['title'],'A')
        self.store.correction(cid,undo_b['revision'],'undo',{})
        self.assertEqual(self.store.detail(cid)['title'],doc['title'])


if __name__=='__main__':unittest.main()
