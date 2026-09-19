"""HTTP regression tests on isolated source fixtures; no cloud or production writes."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mac'))
import dashboard as d
from hemory_local import Store
from semantic.store import SemanticStore


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.source=Store(self.root);self.cid=str(uuid.uuid4());self.audio=b'0123456789'*20
        meta={'chunk_id':self.cid,'session_id':str(uuid.uuid4()),'sequence':0,'started_at':'2026-09-19T00:02:00+08:00',
              'duration_seconds':10,'sha256':'a'*64,'filename':self.cid+'.m4a'}
        with self.source.db() as db:
            db.execute('INSERT INTO chunks(chunk_id,session_id,sequence,metadata,local_date,duration) VALUES(?,?,?,?,?,?)',
                       (self.cid,meta['session_id'],0,json.dumps(meta),'2026-09-19',10))
        path=self.source.audio_path(self.cid);path.parent.mkdir();path.write_bytes(self.audio)
        self.server=d.make_server(self.root,initialize=True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def request(self,path,method='GET',headers=None,body=None):
        con=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        con.request(method,path,body=body,headers=headers or {})
        r=con.getresponse();result=(r.status,dict(r.getheaders()),r.read());con.close();return result
    def test_ranges_and_head(self):
        path='/api/audio/'+self.cid
        for value,expected in [('bytes=0-9',self.audio[:10]),('bytes=-10',self.audio[-10:]),('bytes=195-',self.audio[195:]),('bytes=-1000',self.audio)]:
            status,headers,body=self.request(path,headers={'Range':value})
            self.assertEqual(status,206);self.assertEqual(body,expected)
        for value in ('bytes=-0','bytes=200-','bytes=9-3','bytes=0-2,5-8','bytes=foo','bytes=--1'):
            self.assertEqual(self.request(path,headers={'Range':value})[0],416)
        status,headers,body=self.request(path,'HEAD');self.assertEqual(status,200);self.assertEqual(body,b'');self.assertEqual(int(headers['Content-Length']),200)
    def test_audio_path_boundary_and_unknown_id(self):
        for path in ('/api/audio//tmp/private','/api/audio/../private','/api/audio/%2fetc%2fsecret'):
            self.assertEqual(self.request(path)[0],400)
        self.assertEqual(self.request('/api/audio/'+str(uuid.uuid4()))[0],404)
        path=self.source.audio_path(self.cid);path.unlink();outside=self.root/'outside.m4a';outside.write_bytes(b'secret');path.symlink_to(outside)
        self.assertEqual(self.request('/api/audio/'+self.cid)[0],404)
    def test_host_cross_site_and_headers(self):
        self.assertEqual(self.request('/api/health',headers={'Host':'evil.test'})[0],403)
        self.assertEqual(self.request('/api/health',headers={'Origin':'https://evil.test'})[0],403)
        self.assertEqual(self.request('/api/health',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        status,headers,_=self.request('/api/health');self.assertEqual(status,200)
        self.assertEqual(headers['X-Content-Type-Options'],'nosniff');self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
    def test_get_is_read_only_and_text_failure_distinct(self):
        before=(self.root/'queue.sqlite3').read_bytes()
        self.assertEqual(json.loads(self.request('/api/chunk/'+self.cid)[2])['text_state'],'missing')
        (self.root/'raw'/(self.cid+'.json')).write_text('broken')
        self.assertEqual(json.loads(self.request('/api/chunk/'+self.cid)[2])['text_state'],'invalid')
        result=json.loads(self.request('/api/overview')[2]);self.assertEqual(result['time_zone'],'Asia/Shanghai')
        self.assertEqual(result['counts']['chunks'],1);self.assertEqual(before,(self.root/'queue.sqlite3').read_bytes())
    def test_reprocess_checks_revision_atomically_and_suppresses_cloud(self):
        from test_semantic_store import fixture
        from semantic.conversation import build_conversations
        store=SemanticStore(self.root)
        sources,analyses=fixture();doc=build_conversations(sources,analyses)[0]
        store.publish(doc,doc['input_key'])
        _,headers,data=self.request('/api/session')
        auth={'Cookie':headers['Set-Cookie'].split(';')[0],'X-CSRF-Token':json.loads(data)['csrf'],
              'Content-Type':'application/json','Origin':f'http://127.0.0.1:{self.server.server_port}'}
        payload={'conversation_id':doc['id'],'base_revision':1,'confirm':True,'local_only':True}
        store.correction(doc['id'],1,'rename',{'title':'新版本'})
        self.assertEqual(self.request('/api/reprocess','POST',auth,json.dumps(payload))[0],409)
        with store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM processing_controls').fetchone()[0],0)
        payload['base_revision']=2
        self.assertEqual(self.request('/api/reprocess','POST',auth,json.dumps(payload))[0],200)
        with store.db() as db:self.assertEqual(db.execute('SELECT suppress_cloud FROM processing_controls').fetchone()[0],1)

    def test_overview_lists_conversation_quality_issues(self):
        from test_semantic_store import fixture
        from semantic.conversation import build_conversations
        store=SemanticStore(self.root)
        sources,analyses=fixture();doc=build_conversations(sources,analyses)[0]
        doc['status']='needs_review';doc['summary_review_reasons']=['模型合并遗漏主题，需人工核对']
        store.publish(doc,doc['input_key'])
        attention=d.overview(self.root)['processing']['attention']
        conv=[e for e in attention if e.get('conversation_id')==doc['id']]
        self.assertEqual(len(conv),1)
        self.assertEqual(conv[0]['status'],'needs_review')
        self.assertIn('模型合并遗漏主题',conv[0]['message'])

    def test_edits_require_session_csrf_and_origin(self):
        payload=json.dumps({'conversation_id':str(uuid.uuid4()),'base_revision':1,'operation':'rename','payload':{'title':'x'}})
        self.assertEqual(self.request('/api/corrections','POST',{'Content-Type':'application/json'},payload)[0],403)
        _,headers,data=self.request('/api/session');csrf=json.loads(data)['csrf']
        auth={'Cookie':headers['Set-Cookie'].split(';')[0],'X-CSRF-Token':csrf,'Content-Type':'application/json','Origin':f'http://127.0.0.1:{self.server.server_port}'}
        self.assertEqual(self.request('/api/corrections','POST',auth,payload)[0],400)
        auth['X-CSRF-Token']='bad';self.assertEqual(self.request('/api/corrections','POST',auth,payload)[0],403)


if __name__=='__main__':unittest.main()
