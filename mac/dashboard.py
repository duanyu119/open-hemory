#!/usr/bin/env python3
"""ExtBrain local content dashboard. GET is read-only; explicit edits append revisions."""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import http.cookies
import http.server
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import threading
import time
from urllib.parse import urlsplit, parse_qs
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hemory_local import DEFAULT_DATA, transcript_text
from semantic.store import SemanticStore, Conflict, TZ, now, source_rows, valid_audio, SYNTHETIC_IDS
from semantic.providers import cloud_settings, cloud_status

DEFAULT_HOST, DEFAULT_PORT = '127.0.0.1', 8766
STATIC_DIR = Path(__file__).resolve().parent / 'dashboard'
STATIC_FILES = {'index.html':'text/html; charset=utf-8', 'style.css':'text/css; charset=utf-8',
                'app.js':'application/javascript; charset=utf-8', 'player.js':'application/javascript; charset=utf-8',
                'cards.js':'application/javascript; charset=utf-8'}
STATUS_LABELS={'pending':'等待处理','processing':'旧转写处理中','raw_ready':'旧文字待派生','done':'历史文字已保存',
               'failed':'需核对','needs_review':'需核对','budget_blocked':'预算受限','derive_failed':'派生失败'}


def read_db(root):
    db=sqlite3.connect((Path(root)/'queue.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=10)
    db.row_factory=sqlite3.Row
    return db


def chunk_text(root,cid):
    path=Path(root)/'raw'/(cid+'.json')
    if not path.exists():return '', 'missing'
    try:
        text=transcript_text(json.loads(path.read_text()))
        return text, 'available' if text.strip() else 'empty'
    except (ValueError,OSError,TypeError,KeyError):return '', 'invalid'


def build_chunk(root,row,include_text=False):
    try:
        meta=json.loads(row['metadata']); started=dt.datetime.fromisoformat(meta['started_at']).astimezone(TZ)
    except (ValueError,KeyError,TypeError):
        meta={};started=None
    result={'chunk_id':row['chunk_id'],'session_id':row['session_id'],'sequence':row['sequence'],
            'local_date':started.date().isoformat() if started else row['local_date'],
            'started_at':started.isoformat() if started else None,'started_epoch':started.timestamp() if started else None,
            'duration':row['duration'],'sha256':meta.get('sha256'),'status':row['status'],
            'status_label':STATUS_LABELS.get(row['status'],row['status']),'last_error':row['last_error'],
            'synthetic':row['chunk_id'] in SYNTHETIC_IDS}
    if include_text:
        result['text'],result['text_state']=chunk_text(root,row['chunk_id'])
    return result


def list_chunks(root,date='',cursor='',limit=50):
    limit=min(100,max(1,int(limit)));offset=max(0,int(cursor or 0))
    with contextlib.closing(read_db(root)) as db:
        rows=[build_chunk(root,r) for r in db.execute('SELECT * FROM chunks') if r['chunk_id'] not in SYNTHETIC_IDS]
    if date:rows=[r for r in rows if r['local_date']==date]
    rows.sort(key=lambda r:(r['started_epoch'] or 0,r['chunk_id']),reverse=True)
    return {'chunks':rows[offset:offset+limit], 'next_cursor':str(offset+limit) if len(rows)>offset+limit else None}


def get_chunk(root,cid):
    if str(uuid.UUID(cid))!=cid:raise ValueError('canonical UUID required')
    with contextlib.closing(read_db(root)) as db:
        row=db.execute('SELECT * FROM chunks WHERE chunk_id=?',(cid,)).fetchone()
    return build_chunk(root,row,True) if row else None


def overview(root):
    store=SemanticStore(root)
    sources=[r for r in source_rows(root) if not r['synthetic']]
    days=sorted({r['local_date'] for r in sources},reverse=True)
    counts={'chunks':len(sources),'conversations':0,'audio_seconds':sum(r['duration_seconds'] for r in sources),'speech_seconds':0}
    states={};attention=[];tags=[]
    if store.path.exists():
        with store.db() as db:
            counts['conversations']=db.execute('SELECT count(*) FROM conversations WHERE active=1').fetchone()[0]
            counts['speech_seconds']=db.execute("SELECT coalesce(sum(json_extract(projection,'$.speech_seconds')),0) FROM conversations WHERE active=1").fetchone()[0]
            states={r['state']:r['n'] for r in db.execute('SELECT state,count(*) n FROM analyses GROUP BY state')}
            done=sum(states.values());states['pending']=max(0,len(sources)-done)+states.get('pending',0)
            attention=[dict(r) for r in db.execute("SELECT chunk_id,state,error FROM analyses WHERE state IN ('failed','needs_review') LIMIT 100")]
            attention += [dict(r) for r in db.execute("SELECT target_id,stage,state,error FROM jobs WHERE state IN ('needs_review','failed','budget_blocked') LIMIT 100")]
            # Conversation-level quality issues are a separate dimension from task
            # state; a summary job can succeed while its content is still needs_review.
            for r in db.execute('''SELECT c.id, rv.body FROM conversations c
                    JOIN revisions rv ON rv.conversation_id=c.id AND rv.revision=c.revision
                    WHERE c.active=1 AND c.status='needs_review' LIMIT 100'''):
                reasons=[]
                try:
                    doc=json.loads(r['body'])
                    reasons=[str(x) for x in doc.get('summary_review_reasons',[]) if x]
                except (ValueError,KeyError,TypeError):
                    pass
                attention.append({'conversation_id':r['id'],'state':'needs_review',
                                  'error':('；'.join(reasons) if reasons else '对话内容待核对')})
            tags=[r[0] for r in db.execute('SELECT DISTINCT value FROM conversations,json_each(conversations.tags) WHERE active=1 ORDER BY value')]
    else:states['pending']=len(sources)
    attention = [{**entry, 'status':entry.get('state'), 'message':entry.get('error')} for entry in attention]
    for entry in attention:
        entry['conversation_id'] = entry.get('conversation_id') or entry.get('target_id')
    return {'today':dt.datetime.now(TZ).date().isoformat(),'time_zone':str(TZ),'server_time':now(),'days':days,
            'counts':counts,'processing':{'states':states,'attention':attention},'tags':tags,
            'cloud':cloud_status(store,cloud_settings(root))}


def status_payload(root):
    data=overview(root)
    return {**data,'total':data['counts']['chunks'],'total_duration':data['counts']['audio_seconds'],
            'states':data['processing']['states'],'attention':data['processing']['attention']}


def parse_range(value,total):
    if not value:return 0,total-1,200
    if not value.startswith('bytes=') or ',' in value:raise ValueError('unsupported range')
    spec=value[6:]
    if spec.count('-')!=1:raise ValueError('invalid range')
    start,end=spec.split('-')
    if (start and not start.isascii()) or (end and not end.isascii()):raise ValueError('invalid range')
    if start:
        if not start.isdigit() or (end and not end.isdigit()):raise ValueError('invalid range')
        a=int(start);b=min(int(end),total-1) if end else total-1
    else:
        if not end.isdigit() or int(end)<=0:raise ValueError('invalid suffix')
        a=max(0,total-int(end));b=total-1
    if a<0 or a>=total or b<a:raise ValueError('unsatisfiable range')
    return a,b,206


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    server_version='ExtBrainDashboard/0.2'
    def log_message(self,*_args):pass
    def _headers(self,status,length,ctype,extra=None):
        self.send_response(status)
        for k,v in {'Content-Type':ctype,'Content-Length':str(length),'Cache-Control':'no-store',
                    'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
                    'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                    **(extra or {})}.items():self.send_header(k,v)
        self.end_headers()
    def _send(self,status,body,ctype,extra=None):
        self._headers(status,len(body),ctype,extra)
        if self.command!='HEAD':self.wfile.write(body)
    def _json(self,obj,status=200,extra=None):
        self._send(status,json.dumps(obj,ensure_ascii=False).encode(),'application/json; charset=utf-8',extra)
    def _boundary(self):
        port=self.server.server_port
        hosts={f'127.0.0.1:{port}',f'localhost:{port}'}
        if port==80:hosts|={'127.0.0.1','localhost'}
        host=self.headers.get('Host','').lower()
        origin=self.headers.get('Origin')
        if host not in hosts or (origin and origin!='http://'+host) or self.headers.get('Sec-Fetch-Site')=='cross-site':
            self._json({'error':'仅允许本机同源访问'},403);return False
        return True
    def _static(self,name):
        path=STATIC_DIR/name
        if name not in STATIC_FILES or not path.is_file():self._json({'error':'not found'},404);return
        self._send(200,path.read_bytes(),STATIC_FILES[name])
    def _audio(self,cid):
        if str(uuid.UUID(cid))!=cid:raise ValueError('invalid source ID')
        with contextlib.closing(read_db(self.server.data_dir)) as db:
            if db.execute('SELECT 1 FROM chunks WHERE chunk_id=?',(cid,)).fetchone() is None:
                self._json({'error':'audio not found'},404);return
        try:path=valid_audio(self.server.data_dir,cid)
        except (OSError,ValueError):self._json({'error':'audio unavailable'},404);return
        with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as f:
            st=os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):raise ValueError('not a regular source')
            total=st.st_size
            try:a,b,status=parse_range(self.headers.get('Range'),total)
            except ValueError:
                self._send(416,b'','audio/mp4',{'Content-Range':f'bytes */{total}'});return
            extra={'Accept-Ranges':'bytes'}
            if status==206:extra['Content-Range']=f'bytes {a}-{b}/{total}'
            self._headers(status,b-a+1,'audio/mp4',extra)
            if self.command=='HEAD':return
            f.seek(a);remaining=b-a+1
            while remaining:
                block=f.read(min(65536,remaining))
                if not block:break
                self.wfile.write(block);remaining-=len(block)
    def _session(self):
        cookie=http.cookies.SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except http.cookies.CookieError:return None
        sid=cookie.get('extbrain_session')
        if sid:
            with self.server.session_lock:
                session=self.server.sessions.get(sid.value)
                if session and session['expires']>time.time():return sid.value,session
        return None
    def do_HEAD(self):self.do_GET()
    def do_GET(self):
        if not self._boundary():return
        parsed=urlsplit(self.path);path=parsed.path
        q={k:v[0] for k,v in parse_qs(parsed.query).items()}
        root=self.server.data_dir;store=SemanticStore(root)
        try:
            if path in ('/','/index.html'):self._static('index.html')
            elif path.lstrip('/') in STATIC_FILES:self._static(path.lstrip('/'))
            elif path=='/api/health':self._json({'ok':True})
            elif path=='/api/session':
                current=self._session()
                if current:sid,session=current
                else:
                    sid=secrets.token_urlsafe(32);session={'csrf':secrets.token_urlsafe(32),'expires':time.time()+28800}
                    with self.server.session_lock:
                        self.server.sessions={k:v for k,v in self.server.sessions.items() if v['expires']>time.time()}
                        if len(self.server.sessions)>=128:self.server.sessions.clear()
                        self.server.sessions[sid]=session
                self._json({'csrf':session['csrf']},extra={'Set-Cookie':f'extbrain_session={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'})
            elif path in ('/api/overview','/api/status'):self._json(overview(root) if path.endswith('overview') else status_payload(root))
            elif path=='/api/chunks':self._json(list_chunks(root,q.get('date',''),q.get('cursor',''),q.get('limit',50)))
            elif path.startswith('/api/chunk/'):
                result=get_chunk(root,path[len('/api/chunk/'):]);self._json(result or {'error':'not found'},200 if result else 404)
            elif path.startswith('/api/audio/'):self._audio(path[len('/api/audio/'):])
            elif path in ('/api/conversations','/api/timeline'):
                self._json(store.list(q.get('date',''),q.get('q',''),q.get('topic',''),q.get('cursor',''),q.get('limit',50)))
            elif path.startswith('/api/conversations/'):
                result=store.detail(path[len('/api/conversations/'):],int(q['revision']) if 'revision' in q else None)
                self._json(result or {'error':'not found'},200 if result else 404)
            elif path=='/api/topics':self._json({'topics':overview(root)['tags']})
            elif path.startswith('/api/chunks/') and path.endswith('/regions'):
                cid=path.split('/')[3];analysis=store.analyses().get(cid)
                self._json({'regions':analysis['result'].get('regions',[]) if analysis and analysis['result'] else []})
            else:self._json({'error':'not found'},404)
        except (BrokenPipeError,ConnectionResetError):pass
        except (ValueError,TypeError,KeyError):self._json({'error':'请求参数无效'},400)
        except (sqlite3.Error,OSError):self._json({'error':'本地数据暂时不可用；请保留当前内容并稍后刷新'},503)
    def do_POST(self):
        if not self._boundary():return
        session=self._session()
        if not session or not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),session[1]['csrf']):
            self._json({'error':'编辑会话已失效，请刷新页面'},403);return
        if self.headers.get('Origin')!='http://'+self.headers.get('Host'):
            self._json({'error':'同源校验失败'},403);return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<=0 or length>100000 or self.headers.get('Content-Type','').split(';')[0]!='application/json':
                self._json({'error':'invalid request body'},400);return
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict):raise ValueError('object required')
            store=SemanticStore(self.server.data_dir)
            if self.path=='/api/corrections':
                self._json(store.correction(body['conversation_id'],int(body['base_revision']),body['operation'],body.get('payload',{})))
            elif self.path=='/api/reprocess/preview':
                doc=store.detail(body['conversation_id'])
                if not doc or not doc.get('active'):raise ValueError('conversation missing')
                self._json({'local_only':True,'estimated_max_usd':0,'base_revision':doc['revision'],
                            'chunk_count':len(doc['chunks']),
                            'message':'仅重新分析本地音频；人工修订保留。云摘要需另行核对，不会由本操作自动重发。'})
            elif self.path=='/api/reprocess':
                if body.get('confirm') is not True or body.get('local_only') is not True:
                    raise ValueError('需明确确认仅本地重新分析')
                with store.db(True) as db:
                    db.execute('BEGIN IMMEDIATE')
                    doc=store.detail(body['conversation_id'],db=db)
                    if not doc or not doc.get('active'):raise ValueError('conversation missing')
                    if type(body.get('base_revision')) is not int or body['base_revision']!=doc['revision']:
                        raise Conflict('对话版本已变化，请重新预览范围')
                    for c in doc['chunks']:
                        db.execute("UPDATE analyses SET state='pending' WHERE chunk_id=?",(c['chunk_id'],))
                        db.execute('INSERT INTO processing_controls VALUES(?,?,?) ON CONFLICT(chunk_id) DO UPDATE SET suppress_cloud=excluded.suppress_cloud,reason=excluded.reason',
                                   (c['chunk_id'],1,'用户明确选择仅本地重新分析'))
                self._json({'ok':True,'local_only':True})
            else:self._json({'error':'not found'},404)
        except Conflict as exc:self._json({'error':str(exc)},409)
        except (ValueError,TypeError,KeyError) as exc:self._json({'error':str(exc)[:180]},400)
        except (sqlite3.Error,OSError):self._json({'error':'本地修订暂时无法保存'},503)


def make_server(root,port=0,initialize=False):
    if initialize:SemanticStore(root,initialize=True)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',port),DashboardHandler)
    server.data_dir=Path(root).expanduser().resolve();server.daemon_threads=True
    server.sessions={};server.session_lock=threading.Lock()
    return server


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=DEFAULT_DATA)
    parser.add_argument('--host',choices=['127.0.0.1','localhost'],default=DEFAULT_HOST)
    parser.add_argument('--port',type=int,default=DEFAULT_PORT)
    args=parser.parse_args()
    if not (args.data_dir/'queue.sqlite3').exists():raise SystemExit('queue.sqlite3 not found')
    server=make_server(args.data_dir,args.port,initialize=True)
    print(f'ExtBrain local dashboard: http://127.0.0.1:{args.port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
