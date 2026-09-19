"""Versioned derived data. Original queue/audio are read only to this module."""
from __future__ import annotations
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
SYNTHETIC_IDS = {'25422769-e1bf-4ab9-8e28-8946ebf96a2c', '93deb6e2-84ea-47f6-abac-4453c5df1ec0'}


def now():
    return dt.datetime.now(TZ).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def stable_id(value):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'extbrain:' + value))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8') as f:
            os.chmod(temp, 0o600)
            f.write(encode(value))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def source_rows(root):
    """Never instantiate the legacy Store (its constructor performs recovery)."""
    with contextlib.closing(sqlite3.connect((Path(root) / 'queue.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        result = []
        for row in db.execute('SELECT * FROM chunks'):
            try:
                meta = json.loads(row['metadata'])
                if str(uuid.UUID(meta['chunk_id'])) != meta['chunk_id']:
                    continue
                started = dt.datetime.fromisoformat(meta['started_at'])
                if started.tzinfo is None:
                    continue
                result.append({**meta, 'duration_seconds': row['duration'],
                               'legacy_status': row['status'], 'local_date': started.astimezone(TZ).date().isoformat(),
                               'epoch': started.timestamp(), 'synthetic': row['chunk_id'] in SYNTHETIC_IDS})
            except (ValueError, KeyError, TypeError):
                continue
    return sorted(result, key=lambda c: (c['epoch'], c['session_id'], c['sequence'], c['chunk_id']))


def valid_audio(root, chunk_id):
    if str(uuid.UUID(chunk_id)) != chunk_id:
        raise ValueError('canonical UUID required')
    base = (Path(root) / 'chunks').resolve()
    path = base / chunk_id / (chunk_id + '.m4a')
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(base) or resolved != path or not path.is_file():
        raise ValueError('audio path outside canonical source directory')
    return path


class Conflict(ValueError):
    pass


def recompute(doc):
    """Recompute source-derived fields after a human split/merge, never invent summaries."""
    utterances = doc.get('utterances', [])
    known = {u['id'] for u in utterances}
    chunks = {c['chunk_id']: c for c in doc.get('chunks', [])}
    used = {s['chunk_id'] for u in utterances for s in u['source_spans']}
    doc['chunks'] = sorted([c for cid, c in chunks.items() if cid in used], key=lambda c: c['started_at'])
    ranges = []
    for u in utterances:
        for span in u['source_spans']:
            c = chunks[span['chunk_id']]
            start = dt.datetime.fromisoformat(c['started_at']).timestamp()
            ranges.append((start + span['start_ms'] / 1000, start + span['end_ms'] / 1000, u.get('hidden', False)))
    if ranges:
        doc['started_at'] = dt.datetime.fromtimestamp(min(s for s, _, _ in ranges), TZ).isoformat()
        doc['ended_at'] = dt.datetime.fromtimestamp(max(e for _, e, _ in ranges), TZ).isoformat()
        doc['duration_seconds'] = max(e for _, e, _ in ranges) - min(s for s, _, _ in ranges)
        intervals = sorted((s, e) for s, e, h in ranges if not h)
        merged = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(end, merged[-1][1])
            else:
                merged.append([start, end])
        doc['speech_seconds'] = round(sum(e - s for s, e in merged), 3)
    doc['regions'] = [r for r in doc.get('regions', []) if r['chunk_id'] in used]
    for topic in doc.get('topics', []):
        topic['utterance_ids'] = [i for i in topic['utterance_ids'] if i in known]
        topic['key_points'] = [p for p in topic.get('key_points', []) if set(p.get('evidence_ids', [])) <= known]
    doc['topics'] = [t for t in doc.get('topics', []) if t['utterance_ids']]
    doc['topic_count'] = len(doc['topics'])
    doc['tags'] = [t['title'] for t in doc['topics']][:12]
    return doc


class SemanticStore:
    def __init__(self, root, initialize=False):
        self.root = Path(root).expanduser().resolve()
        self.directory = self.root / 'semantic'
        self.path = self.directory / 'index.sqlite3'
        if initialize:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.db(write=True) as db:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version not in (0, 1):
                    raise ValueError('unsupported semantic database version')
                db.executescript('''
                CREATE TABLE IF NOT EXISTS processing_controls (
                    chunk_id TEXT PRIMARY KEY, suppress_cloud INTEGER NOT NULL, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analyses (
                    chunk_id TEXT PRIMARY KEY, input_key TEXT NOT NULL, state TEXT NOT NULL,
                    result TEXT, error TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, stage TEXT NOT NULL, input_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL, target_id TEXT, error TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, active INTEGER NOT NULL,
                    manual INTEGER NOT NULL, input_key TEXT NOT NULL,
                    started_at TEXT NOT NULL, ended_at TEXT NOT NULL, date TEXT NOT NULL,
                    title TEXT NOT NULL, overview TEXT NOT NULL, tags TEXT NOT NULL,
                    status TEXT NOT NULL, search_text TEXT NOT NULL, projection TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS conversation_order ON conversations(active,started_at DESC,id);
                CREATE INDEX IF NOT EXISTS conversation_date ON conversations(date,active,started_at DESC);
                CREATE TABLE IF NOT EXISTS revisions (
                    conversation_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
                    created_at TEXT NOT NULL, reason TEXT NOT NULL,
                    PRIMARY KEY(conversation_id,revision));
                CREATE TABLE IF NOT EXISTS corrections (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, operation TEXT NOT NULL,
                    before_json TEXT NOT NULL, after_versions TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS provider_attempts (
                    id TEXT PRIMARY KEY, input_key TEXT NOT NULL UNIQUE, month TEXT NOT NULL,
                    reserved_usd REAL NOT NULL, actual_usd REAL, state TEXT NOT NULL,
                    model TEXT NOT NULL, price_json TEXT NOT NULL, usage_json TEXT,
                    created_at TEXT NOT NULL, error TEXT);
                PRAGMA user_version=1;
                ''')
            os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def db(self, write=False):
        uri = str(self.path) if write else self.path.as_uri() + '?mode=ro'
        db = sqlite3.connect(uri, uri=not write, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        if write:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def analyses(self):
        if not self.path.exists():
            return {}
        with self.db() as db:
            return {r['chunk_id']: {**dict(r), 'result': json.loads(r['result']) if r['result'] else None}
                    for r in db.execute('SELECT * FROM analyses')}

    def set_analysis(self, chunk_id, key, state, result=None, error=None):
        with self.db(True) as db:
            db.execute('INSERT INTO analyses VALUES(?,?,?,?,?,?) ON CONFLICT(chunk_id) DO UPDATE SET input_key=excluded.input_key,state=excluded.state,result=excluded.result,error=excluded.error,updated_at=excluded.updated_at',
                       (chunk_id, key, state, encode(result) if result is not None else None, error, now()))

    def detail(self, cid, revision=None, db=None):
        if not self.path.exists():
            return None
        if db is None:
            with self.db() as con:
                return self.detail(cid, revision, con)
        row = db.execute('SELECT * FROM conversations WHERE id=?', (cid,)).fetchone()
        if row is None:
            return None
        rev = revision if revision is not None else row['revision']
        body = db.execute('SELECT body FROM revisions WHERE conversation_id=? AND revision=?', (cid, rev)).fetchone()
        if body is None:
            return None
        return {**json.loads(body[0]), 'revision': rev, 'active': bool(row['active']), 'manual': bool(row['manual'])}

    def _publish(self, db, doc, input_key, reason, active=True, manual=False):
        old = db.execute('SELECT revision FROM conversations WHERE id=?', (doc['id'],)).fetchone()
        revision = old[0] + 1 if old else 1
        body = {**doc, 'revision': revision, 'manual': manual}
        projection_keys = ('id', 'revision', 'title', 'overview', 'started_at', 'ended_at', 'duration_seconds',
                           'speech_seconds', 'tags', 'topic_count', 'status', 'boundary_status', 'manual')
        projection = {k: body.get(k) for k in projection_keys}
        date = dt.datetime.fromisoformat(body['started_at']).astimezone(TZ).date().isoformat()
        search = ' '.join([body['title'], body['overview']] + [u['text'] for u in body.get('utterances', [])])
        db.execute('INSERT INTO revisions VALUES(?,?,?,?,?)', (body['id'], revision, encode(body), now(), reason))
        db.execute('INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,active=excluded.active,manual=excluded.manual,input_key=excluded.input_key,started_at=excluded.started_at,ended_at=excluded.ended_at,date=excluded.date,title=excluded.title,overview=excluded.overview,tags=excluded.tags,status=excluded.status,search_text=excluded.search_text,projection=excluded.projection',
                   (body['id'], revision, int(active), int(manual), input_key, body['started_at'], body['ended_at'], date,
                    body['title'], body['overview'], encode(body.get('tags', [])), body['status'], search, encode(projection)))
        return body

    def publish(self, doc, key, reason='automatic'):
        with self.db(True) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT manual,input_key FROM conversations WHERE id=?', (doc['id'],)).fetchone()
            if old and (old['manual'] or old['input_key'] == key):
                return False
            self._publish(db, doc, key, reason)
        return True

    def list(self, date='', query='', topic='', cursor='', limit=50):
        if not self.path.exists():
            return {'items': [], 'next_cursor': None}
        limit = min(100, max(1, int(limit)))
        clauses, values = ['active=1'], []
        if date:
            dt.date.fromisoformat(date)
            clauses.append('date=?'); values.append(date)
        if query:
            clauses.append('instr(lower(search_text),lower(?))>0'); values.append(query[:200])
        if topic:
            clauses.append('EXISTS (SELECT 1 FROM json_each(conversations.tags) WHERE value=?)'); values.append(topic[:200])
        if cursor:
            import base64
            start, cid = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            clauses.append('(started_at < ? OR (started_at = ? AND id > ?))'); values += [start, start, cid]
        with self.db() as db:
            rows = db.execute('SELECT id,started_at,projection FROM conversations WHERE ' + ' AND '.join(clauses) + ' ORDER BY started_at DESC,id LIMIT ?', [*values, limit + 1]).fetchall()
        next_cursor = None
        if len(rows) > limit:
            import base64
            next_cursor = base64.urlsafe_b64encode(encode([rows[limit-1]['started_at'], rows[limit-1]['id']]).encode()).decode()
        return {'items': [json.loads(r['projection']) for r in rows[:limit]], 'next_cursor': next_cursor}

    def correction(self, cid, base_revision, operation, payload):
        with self.db(True) as db:
            db.execute('BEGIN IMMEDIATE')
            doc = self.detail(cid, db=db)
            if not doc or not doc['active']:
                raise ValueError('conversation not available')
            if doc['revision'] != base_revision:
                raise Conflict('内容已更新，请刷新后再修改')
            if operation == 'undo':
                event = db.execute('SELECT * FROM corrections WHERE conversation_id=? AND undone=0 ORDER BY created_at DESC,id DESC LIMIT 1', (cid,)).fetchone()
                if event is None:
                    raise ValueError('没有可撤销的修改')
                before = json.loads(event['before_json']); after = json.loads(event['after_versions'])
                for affected, expected in after.items():
                    current = self.detail(affected, db=db)
                    if not current or current['revision'] != expected:
                        raise Conflict('关联内容已修改，不能直接撤销')
                restored_versions = {}
                for affected, snapshot in before.items():
                    if snapshot is None:
                        db.execute('UPDATE conversations SET active=0 WHERE id=?', (affected,))
                    else:
                        restored = self._publish(db, snapshot, fingerprint(snapshot), 'undo', snapshot['active'], snapshot['manual'])
                        restored_versions[affected] = (snapshot['revision'], restored['revision'])
                # An undo creates a new revision of an identical historical state.
                # Advance only expectations that referred exactly to that restored state;
                # unrelated edits to a merge/split participant must still conflict.
                for earlier in db.execute('SELECT id,after_versions FROM corrections WHERE undone=0 AND id<>?', (event['id'],)).fetchall():
                    expected = json.loads(earlier['after_versions']); changed = False
                    for affected, (old_revision, new_revision) in restored_versions.items():
                        if expected.get(affected) == old_revision:
                            expected[affected] = new_revision; changed = True
                    if changed:
                        db.execute('UPDATE corrections SET after_versions=? WHERE id=?', (encode(expected), earlier['id']))
                db.execute('UPDATE corrections SET undone=1 WHERE id=?', (event['id'],))
                updated = self.detail(cid, db=db)
                return {'ok': True, 'id': cid, 'revision': updated['revision']}
            before = {cid: copy.deepcopy(doc)}
            changes = {cid: doc}
            if operation == 'rename':
                title = str(payload.get('title', '')).strip()
                if not title or len(title) > 160: raise ValueError('标题需为1–160字')
                doc['title'] = title
            elif operation in ('edit_utterance', 'hide_utterance'):
                target = next((u for u in doc['utterances'] if u['id'] == payload.get('utterance_id')), None)
                if target is None: raise ValueError('发言不存在')
                if operation == 'edit_utterance':
                    text = str(payload.get('text', '')).strip()
                    if not text or len(text) > 10000: raise ValueError('文字长度不正确')
                    target['text'] = text
                    doc['status'] = 'needs_review'
                    doc['summary_stale'] = True
                else:
                    if not isinstance(payload.get('hidden'), bool): raise ValueError('hidden must be boolean')
                    target['hidden'] = payload['hidden']
                recompute(doc)
            elif operation == 'merge':
                other_id = payload.get('other_id')
                if other_id == cid: raise ValueError('不能与自身合并')
                other = self.detail(other_id, db=db)
                if not other or not other['active']: raise ValueError('目标对话不存在')
                if other['revision'] != payload.get('other_revision'): raise Conflict('目标对话已更新')
                before[other_id] = copy.deepcopy(other)
                for key, idkey in (('utterances', 'id'), ('chunks', 'chunk_id'), ('topics', 'id')):
                    doc[key] = list({i[idkey]: i for i in doc[key] + other[key]}.values())
                doc['regions'] += other['regions']
                chunk_times = {c['chunk_id']: dt.datetime.fromisoformat(c['started_at']).timestamp() for c in doc['chunks']}
                doc['utterances'].sort(key=lambda u: chunk_times[u['source_spans'][0]['chunk_id']] + u['source_spans'][0]['start_ms']/1000)
                other['active'] = False; changes[other_id] = other
                doc['overview'] = '已手动合并；请核对主题与原文。'
                doc['status'] = 'needs_review'; doc['summary_stale'] = True
                recompute(doc)
            elif operation == 'split':
                idx = next((i for i,u in enumerate(doc['utterances']) if u['id'] == payload.get('at_utterance_id')), -1)
                if idx <= 0: raise ValueError('请选择第一条之后的分割位置')
                other = copy.deepcopy(doc); other_id = str(uuid.uuid4()); other['id'] = other_id
                other['utterances'] = other['utterances'][idx:]; doc['utterances'] = doc['utterances'][:idx]
                before[other_id] = None; changes[other_id] = other
                for part in (doc, other):
                    part['title'] = '待整理的对话'; part['overview'] = '已手动拆分，摘要待核对。'
                    part['key_points'] = []; part['decisions'] = []; part['action_candidates'] = []; part['open_questions'] = []
                    part['status'] = 'needs_review'; part['summary_stale'] = True
                    recompute(part)
            elif operation in ('rename_topic', 'merge_topics', 'split_topic'):
                topics = {t['id']: t for t in doc['topics']}
                if operation == 'rename_topic':
                    target = topics.get(payload.get('topic_id')); title = str(payload.get('title','')).strip()
                    if not target or not title or len(title)>160: raise ValueError('主题或标题不正确')
                    target['title'] = title
                elif operation == 'merge_topics':
                    ids = list(dict.fromkeys(payload.get('topic_ids', [])))
                    if len(ids)<2 or any(i not in topics for i in ids): raise ValueError('请选择至少两个有效主题')
                    target = topics[ids[0]]
                    target['utterance_ids'] = list(dict.fromkeys(i for tid in ids for i in topics[tid]['utterance_ids']))
                    target['key_points'] = [p for tid in ids for p in topics[tid].get('key_points', [])]
                    doc['topics'] = [t for t in doc['topics'] if t['id'] not in ids[1:]]
                else:
                    target = topics.get(payload.get('topic_id')); ids = set(payload.get('utterance_ids', []))
                    if not target or not ids or not ids < set(target['utterance_ids']): raise ValueError('拆分需选择主题中的部分发言')
                    title = str(payload.get('title', '新主题')).strip()[:160] or '新主题'
                    doc['topics'].append({'id': str(uuid.uuid4()), 'title': title, 'key_points': [], 'utterance_ids': [i for i in target['utterance_ids'] if i in ids]})
                    target['utterance_ids'] = [i for i in target['utterance_ids'] if i not in ids]
                    target['key_points'] = []
                recompute(doc)
            else:
                raise ValueError('不支持的修改操作')
            versions = {}
            for affected, changed in changes.items():
                saved = self._publish(db, changed, fingerprint(changed), operation, changed.get('active', True), True)
                versions[affected] = saved['revision']
            db.execute('INSERT INTO corrections VALUES(?,?,?,?,?,?,?)',
                       (str(uuid.uuid4()), cid, operation, encode(before), encode(versions), 0, now()))
            return {'ok': True, 'id': cid, 'revision': versions[cid]}
