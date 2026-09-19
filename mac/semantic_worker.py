#!/usr/bin/env python3
"""Single local semantic worker. Shares worker.lock with the legacy cloud STT worker."""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
from hemory_local import DEFAULT_DATA
from semantic.store import SemanticStore, source_rows, fingerprint, atomic_json, valid_audio, now, encode, stable_id, recompute
from semantic.conversation import build_conversations
from semantic.providers import CloudBlocked, CloudUncertain
from semantic.summarize import summarize


def load_config(root,path=None):
    path=path or root/'private'/'semantic.json'
    if not path.exists():return {'local':{},'cloud':{'enabled':False}}
    return json.loads(path.read_text())


def validate_result(result,source):
    duration=result['duration_ms']
    if duration<=0 or abs(duration-source['duration_seconds']*1000)>2500:raise ValueError('media duration mismatch')
    for r in result['regions']:
        if not 0<=r['start_ms']<r['end_ms']<=duration:raise ValueError('invalid region')
        if r['kind'] not in ('speech','silence','uncertain','non_speech','mixed'):raise ValueError('invalid region class')
    seen=set()
    for u in result['utterances']:
        if not u['id'] or u['id'] in seen or not isinstance(u['text'],str) or not u['text'].strip():raise ValueError('invalid utterance')
        seen.add(u['id'])
        if not u['source_spans']:raise ValueError('missing source')
        for span in u['source_spans']:
            if span['chunk_id']!=source['chunk_id'] or span['sha256']!=source['sha256'] or not 0<=span['start_ms']<span['end_ms']<=duration:
                raise ValueError('invalid source span')


def _overlapping_spans(a, b):
    return (a['chunk_id'] == b['chunk_id'] and a['sha256'] == b['sha256']
            and a['start_ms'] < b['end_ms'] and b['start_ms'] < a['end_ms'])


def manual_protection(documents):
    ids, spans = set(), {}
    for doc in documents:
        for utterance in doc['utterances']:
            ids.add(utterance['id'])
            for span in utterance['source_spans']:
                spans.setdefault(span['chunk_id'], []).append(span)
    return ids, spans


def touches_manual(utterance, protection):
    ids, spans = protection
    return (utterance['id'] in ids or any(
        _overlapping_spans(span, old)
        for span in utterance['source_spans'] for old in spans.get(span['chunk_id'], [])))


def boundary_view(store, sources, analyses, local, model_identity, reconciler, manual_docs=()):
    """Cache original pair results; apply only safe replacements to a disposable view."""
    view = copy.deepcopy(analyses)
    protection = manual_protection(manual_docs)
    boundary_hash = hashlib.sha256((Path(__file__).parent / 'semantic' / 'boundary.py').read_bytes()).hexdigest()
    ordered = sorted(sources, key=lambda s: (s['epoch'], s['chunk_id']))
    replaced, accepted, additions = set(), [], {}
    changed = {}
    for left, right in zip(ordered, ordered[1:]):
        pair = [left, right]
        originals = {s['chunk_id']: analyses.get(s['chunk_id'], {}) for s in pair}
        identity = {
            'sources': [{k: s.get(k) for k in ('chunk_id', 'sha256', 'epoch', 'duration_seconds',
                                               'session_id', 'sequence', 'synthetic')} for s in pair],
            'analyses': {cid: {'input_key': entry.get('input_key'), 'state': entry.get('state'),
                              'content': {k: (entry.get('result') or {}).get(k)
                                          for k in ('duration_ms', 'regions', 'utterances')}}
                         for cid, entry in originals.items()},
            'local': local, 'model_identity': model_identity, 'boundary_sha256': boundary_hash,
            'cache_version': 1,
        }
        cache_key = fingerprint(identity)
        directory = store.directory / 'runs' / cache_key
        cache = directory / 'boundary.json'
        try:
            result = json.loads(cache.read_text())
            if result.get('input_key') != cache_key or not isinstance(result.get('result'), dict):
                raise ValueError('invalid boundary cache')
            result = result['result']
        except (OSError, ValueError):
            # Explicit injected reconciler is the only boundary execution path for
            # injected-analyzer tests. Production gets the lazy local reconciler.
            result = reconciler(copy.deepcopy(pair), copy.deepcopy(originals), local, store.root)
            atomic_json(cache, {'input_key': cache_key, 'identity': identity, 'result': result})
        original_us = [u for entry in originals.values() if entry.get('state') == 'succeeded'
                       for u in entry['result']['utterances']]
        known = {u['id'] for u in original_us}
        candidates = []
        for value in result.get('utterances', []):
            u = copy.deepcopy(value)
            ids = set(u.get('replaces_ids', []))
            safe = (u.get('boundary_status') == 'reconciled' and ids and ids <= known
                    and ids <= set(result.get('replaced_ids', [])))
            if safe and (ids & replaced or any(
                    _overlapping_spans(a, b) for old in accepted
                    for a in u['source_spans'] for b in old['source_spans'])):
                safe = False
                u['reason'] = 'conflicting_cached_boundaries'
            if safe and (touches_manual(u, protection) or any(
                    touches_manual(old, protection) for old in original_us if old['id'] in ids)):
                safe = False
                u['reason'] = 'manual_revision_requires_review'
            if not safe:
                u.update(boundary_status='context_candidate', review_required=True, replaces_ids=[])
                candidates.append(u)
                continue
            anchor = u['source_spans'][0]['chunk_id']
            if anchor not in originals:
                raise ValueError('boundary anchor outside source pair')
            replaced.update(ids)
            accepted.append(u)
            additions.setdefault(anchor, []).append(u)
            for source in pair:
                changed.setdefault(source['chunk_id'], []).append({'cache_key': cache_key, 'utterance_id': u['id']})
        # Persist a separate review collection. It is never passed to conversation
        # construction or summarization, including after a cache hit or human edit.
        if candidates:
            review = {'input_key': cache_key, 'utterances': candidates}
            review_path = directory / 'boundary-review.json'
            if not review_path.exists() or review_path.read_text() != encode(review):
                atomic_json(review_path, review)
    for cid, changes in changed.items():
        entry = view[cid]
        entry['result']['utterances'] = [u for u in entry['result']['utterances'] if u['id'] not in replaced]
        entry['result']['utterances'].extend(additions.get(cid, []))
        entry['input_key'] = fingerprint({'analysis_key': analyses[cid]['input_key'], 'boundaries': changes})
    return view


def publish_local(store, doc):
    """Keep row and revision input_key tied to the local view, including legacy rows."""
    key = doc['input_key']
    with store.db(True) as db:
        db.execute('BEGIN IMMEDIATE')
        current = store.detail(doc['id'], db=db)
        if current and current['manual']:
            return current
        if current and current['active'] and current.get('input_key') == key:
            # Earlier worker versions put summary_key only in the index row.
            db.execute('UPDATE conversations SET input_key=? WHERE id=?', (key, doc['id']))
            return current
        store._publish(db, doc, key, 'automatic')
        return store.detail(doc['id'], db=db)


def publish_summary(store, output, local_key, summary_key, base_revision):
    with store.db(True) as db:
        db.execute('BEGIN IMMEDIATE')
        current = store.detail(output['id'], db=db)
        if (not current or not current['active'] or current['manual']
                or current['revision'] != base_revision or current.get('input_key') != local_key):
            return False
        body = {**output, 'input_key': local_key, 'summary_key': summary_key}
        store._publish(db, body, local_key, 'summary')
    return True


def process(store,config,limit=0,no_cloud=False,analyzer=None,boundary_reconciler=None):
    sources=source_rows(store.root)
    analyses=store.analyses()
    local=config.get('local',{})
    model_identity = {}
    use_default_boundary = analyzer is None
    if analyzer is None:
        from semantic.transcribe import local_model_identity
        model_identity = local_model_identity(local)
        with Path(local['vad_model_path']).open('rb') as vad_file:
            model_identity['vad_sha256'] = hashlib.file_digest(vad_file, 'sha256').hexdigest()
    code_identity = {name: hashlib.sha256((Path(__file__).parent / 'semantic' / name).read_bytes()).hexdigest()
                     for name in ('audio.py', 'timeline.py', 'transcribe.py')}
    count=0
    for source in sources:
        if source['synthetic']:continue
        key=fingerprint({'source':{k:source[k] for k in ('chunk_id','sha256','started_at','duration_seconds')},'local':local,'model_identity':model_identity,'code_identity':code_identity,'pipeline_version':1})
        old=analyses.get(source['chunk_id'])
        if old and old['input_key']==key and old['state'] in ('succeeded','failed','needs_review'):continue
        if limit and count>=limit:break
        count+=1
        store.set_analysis(source['chunk_id'],key,'running')
        try:
            path=valid_audio(store.root,source['chunk_id'])
            with path.open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
            if actual!=source['sha256']:raise ValueError('source integrity mismatch')
            if analyzer is None:
                from semantic.audio import analyze_chunk
                analyzer=analyze_chunk
            started=time.monotonic()
            result=analyzer(path,source,local)
            validate_result(result,source)
            result.setdefault('metrics',{})['elapsed_seconds']=round(time.monotonic()-started,3)
            artifact=store.directory/'runs'/key/'analysis.json'
            atomic_json(artifact,{'input':{k:source[k] for k in ('chunk_id','sha256','started_at','duration_seconds')},'config':local,'result':result})
            store.set_analysis(source['chunk_id'],key,'succeeded',result)
            print(json.dumps({'stage':'local','chunk_id':source['chunk_id'],'state':'succeeded','utterances':len(result['utterances']),'elapsed_seconds':result['metrics']['elapsed_seconds']}),flush=True)
        except Exception as exc:
            store.set_analysis(source['chunk_id'],key,'failed',error=type(exc).__name__+'; 本地分析未完成，原音保留')
            print(json.dumps({'stage':'local','chunk_id':source['chunk_id'],'state':'failed','error_type':type(exc).__name__}),flush=True)
    analyses=store.analyses()
    with store.db() as db:
        manual_docs=[store.detail(r['id'],db=db) for r in db.execute('SELECT id FROM conversations WHERE active=1 AND manual=1')]
        cloud_suppressed = {r[0] for r in db.execute('SELECT chunk_id FROM processing_controls WHERE suppress_cloud=1')}
    if boundary_reconciler is None and use_default_boundary:
        from semantic.boundary import reconcile_boundaries
        boundary_reconciler = reconcile_boundaries
    if boundary_reconciler is not None:
        analyses = boundary_view(store, sources, analyses, local, model_identity, boundary_reconciler, manual_docs)
    from semantic.quality import annotate_utterances, read_reviews
    reviews=read_reviews(store.directory)
    for entry in analyses.values():
        if entry.get('state')=='succeeded':
            entry['result']['utterances']=annotate_utterances(entry['result']['utterances'],reviews)
    # A retry can change state or acoustic regions without changing words or the
    # original analysis input key. Include those changes in the conversation view
    # identity, but never runtime metrics or the persisted source analysis row.
    analyses = {cid: {**entry, 'input_key': fingerprint({
        'analysis_key': entry['input_key'], 'state': entry['state'],
        'content': {k: (entry.get('result') or {}).get(k) for k in ('duration_ms', 'regions', 'utterances')},
    })} for cid, entry in analyses.items()}
    documents=build_conversations(sources,analyses)
    current_ids=set()
    protection=manual_protection(manual_docs)
    manual_chunks={c['chunk_id'] for d in manual_docs for c in d['chunks']}
    for doc in documents:
        old=store.detail(doc['id'])
        protected = [u for u in doc['utterances'] if touches_manual(u, protection)]
        if protected or (old and old['manual']):
            # IDs can change after retranscription. Source overlap also protects a
            # human correction; genuinely new late-upload spans remain visible.
            atomic_json(store.directory/'runs'/doc['input_key']/'conversation-candidate.json',doc)
            fresh = [u for u in doc['utterances'] if not touches_manual(u, protection)]
            new_chunks = []
            for c in doc['chunks']:
                if c['chunk_id'] in manual_chunks:
                    continue
                entry = analyses.get(c['chunk_id'], {})
                regions = (entry.get('result') or {}).get('regions', [])
                confirmed_quiet = (entry.get('state') == 'succeeded' and regions
                                   and all(r['kind'] in ('silence', 'non_speech') for r in regions))
                if not confirmed_quiet:
                    new_chunks.append(c)
            if not fresh and not new_chunks:
                continue
            doc = copy.deepcopy(doc)
            doc['utterances'] = fresh
            anchor = fresh[0]['id'] if fresh else 'source:' + new_chunks[0]['chunk_id']
            doc['id'] = stable_id('supplement:' + anchor)
            doc['title'] = '补充录音 · 待整理' if fresh else '补充录音 · 文字待核对'
            doc['overview'] = '原对话已人工整理；新到达的内容单独保留，可核对后合并。'
            doc['input_key'] = fingerprint({'supplement': fresh, 'source_key': doc['input_key']})
            all_chunks, all_regions = doc['chunks'], doc['regions']
            recompute(doc)
            # A late source with failed/uncertain ASR still needs a reachable card.
            # recompute alone retains only chunks referenced by recognized words.
            retained = {c['chunk_id'] for c in doc['chunks']} | {c['chunk_id'] for c in new_chunks}
            doc['chunks'] = [c for c in all_chunks if c['chunk_id'] in retained]
            doc['regions'] = [r for r in all_regions if r['chunk_id'] in retained]
            if new_chunks:
                first = min(dt.datetime.fromisoformat(c['started_at']) for c in doc['chunks'])
                last = max(dt.datetime.fromisoformat(c['started_at']) + dt.timedelta(seconds=c['duration'])
                           for c in doc['chunks'])
                doc.update(started_at=first.isoformat(), ended_at=last.isoformat(), duration_seconds=(last-first).total_seconds())
            if not fresh:
                doc.update(status='needs_review', speech_seconds=0)
        current_ids.add(doc['id'])
        key=doc['input_key']
        old=publish_local(store,doc)
        if old['manual'] or old['status']=='ready':continue
        if (doc['status']!='transcribed' or no_cloud or not config.get('cloud',{}).get('enabled')
                or cloud_suppressed & {c['chunk_id'] for c in doc['chunks']}):
            continue
        summary_key=fingerprint({'input_key':key,'model':config['cloud'].get('model'),'prompt':2})
        with store.db() as db:
            job=db.execute('SELECT state FROM jobs WHERE input_key=?',(summary_key,)).fetchone()
        if job and job['state'] in ('succeeded','needs_review','failed','budget_blocked'):continue
        with store.db(True) as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?) ON CONFLICT(input_key) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at',
                       (summary_key,'topic_summary',summary_key,'running',doc['id'],None,now()))
        state='succeeded';error=None
        try:
            output=summarize(store,doc,config['cloud'])
            output.update(input_key=key, summary_key=summary_key)
            atomic_json(store.directory/'runs'/summary_key/'summary.json',output)
            # Summary identity is separate from source identity; a concurrent human
            # edit or source revision must win over this in-flight result.
            published = publish_summary(store,output,key,summary_key,old['revision'])
            if published:
                # A concurrent human edit wins; unpublished candidates stay only in runs/.
                export=store.directory/'exports'/(doc['id']+'.md')
                export.parent.mkdir(exist_ok=True,mode=0o700)
                lines=['# '+output['title'],'',output['overview'],'']
                for topic in output['topics']:
                    lines.extend(['## '+topic['title'],'']+['- '+p['text'] for p in topic['key_points']]+[''])
                export.write_text('\n'.join(lines))
        except CloudBlocked as exc:state='budget_blocked';error=str(exc)
        except CloudUncertain as exc:state='needs_review';error=type(exc).__name__+'; 摘要请求未确认，未替换原文'
        except Exception as exc:
            # Any malformed model output (incl. non-dict topics, wrong field types)
            # must fail only this task and never stop the worker loop. Never catch
            # BaseException: KeyboardInterrupt/SystemExit still propagate.
            state='needs_review';error=type(exc).__name__+'; 摘要未通过验证，未替换原文'
        with store.db(True) as db:db.execute('UPDATE jobs SET state=?,error=?,updated_at=? WHERE input_key=?',(state,error,now(),summary_key))
        print(json.dumps({'stage':'summary','conversation_id':doc['id'],'state':state}),flush=True)
    with store.db(True) as db:
        # Retire obsolete automatic candidates only after supplemental IDs are known.
        for row in db.execute('SELECT id FROM conversations WHERE active=1 AND manual=0').fetchall():
            if row['id'] not in current_ids:
                db.execute('UPDATE conversations SET active=0 WHERE id=?',(row['id'],))
    return count


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=DEFAULT_DATA)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--no-cloud',action='store_true')
    parser.add_argument('--limit',type=int,default=0)
    args=parser.parse_args();root=args.data_dir.expanduser().resolve()
    if not (root/'queue.sqlite3').exists():raise SystemExit('queue database missing')
    with (root/'worker.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('Another worker owns worker.lock')
        store=SemanticStore(root,initialize=True)
        # A local interrupted run is safe to recompute. An unknown paid request is not.
        with store.db(True) as db:
            db.execute("UPDATE analyses SET state='pending' WHERE state='running'")
            db.execute("UPDATE jobs SET state='needs_review',error='worker interrupted; inspect saved response before retry' WHERE state='running'")
        while True:
            config=load_config(root,args.config)
            if not config.get('paused',False):
                process(store,config,args.limit or max(1,int(config.get('local_batch_size',4))),args.no_cloud)
            if args.once:break
            time.sleep(15)


if __name__=='__main__':main()
