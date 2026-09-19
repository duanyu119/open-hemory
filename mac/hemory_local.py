#!/usr/bin/env python3
"""Private Watch audio receiver and conservative cloud-transcription queue (stdlib only)."""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import http.server
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from zoneinfo import ZoneInfo

MAX_BYTES = 24_000_000
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DATA = Path.home() / "Library/Application Support/HemoryLocal"
PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1/audio/transcriptions", "whisper-large-v3-turbo"),
    "siliconflow": ("https://api.siliconflow.cn/v1/audio/transcriptions", "FunAudioLLM/SenseVoiceSmall"),
    "openai": ("https://api.openai.com/v1/audio/transcriptions", "gpt-4o-mini-transcribe"),
}
FIELDS = {"chunk_id", "session_id", "started_at", "duration_seconds", "sequence", "sha256", "filename"}


class Rejected(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_metadata(meta):
    if not isinstance(meta, dict) or set(meta) != FIELDS:
        raise Rejected(400, "metadata fields do not match protocol")
    for field in ("chunk_id", "session_id"):
        value = meta[field]
        try:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError()
        except (ValueError, AttributeError):
            raise Rejected(400, f"{field} must be a canonical UUID")
    if meta["filename"] != meta["chunk_id"] + ".m4a":
        raise Rejected(400, "filename must be chunk_id.m4a")
    if not isinstance(meta["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", meta["sha256"]):
        raise Rejected(400, "invalid sha256")
    if type(meta["sequence"]) is not int or not 0 <= meta["sequence"] <= 1_000_000:
        raise Rejected(400, "sequence must be a nonnegative integer")
    duration = meta["duration_seconds"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 3600:
        raise Rejected(400, "duration must be 0 < seconds <= 3600")
    try:
        if not isinstance(meta["started_at"], str) or "T" not in meta["started_at"]:
            raise ValueError()
        started = dt.datetime.fromisoformat(meta["started_at"])
        if started.tzinfo is None or started.utcoffset() is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise Rejected(400, "started_at must be an ISO8601 timestamp with timezone")
    return started.astimezone(LOCAL_TZ).date().isoformat()


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data if isinstance(data, bytes) else data.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def atomic_write_once(path, data):
    """Publish a complete immutable artifact, or verify an identical recovery replay."""
    path = Path(path)
    encoded = data if isinstance(data, bytes) else data.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, path)  # Unlike replace(), an existing artifact is never overwritten.
            fsync_dir(path.parent)
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise ValueError("immutable processing artifact differs from saved result")
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def read_secret(path):
    value = Path(path).expanduser().read_text().strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("secret file must contain one nonempty line")
    return value


def probe_audio(path, expected):
    try:
        process = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name",
             "-of", "json", str(path)], capture_output=True, timeout=20, check=True)
        info = json.loads(process.stdout)
        actual = float(info["format"]["duration"])
        streams = info["streams"]
        if not streams or any(s.get("codec_type") != "audio" for s in streams):
            raise ValueError()
        if not math.isfinite(actual) or actual <= 0 or abs(actual - expected) > max(2, expected * 0.02):
            raise ValueError()
        return actual
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
        raise Rejected(422, "audio is invalid or measured duration does not match metadata")


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        for folder in ("chunks", "tmp", "raw", "runs", "transcripts", "daily"):
            (self.root / folder).mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                metadata TEXT NOT NULL, local_date TEXT NOT NULL, duration REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT, provider TEXT,
                UNIQUE(session_id, sequence));
              CREATE TABLE IF NOT EXISTS charges (
                id INTEGER PRIMARY KEY, chunk_id TEXT NOT NULL, month TEXT NOT NULL,
                estimate REAL NOT NULL, provider TEXT NOT NULL, outcome TEXT NOT NULL,
                created_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS run_context (
                charge_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
                chunk_id TEXT NOT NULL, input_sha256 TEXT NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL, started_at TEXT NOT NULL,
                completed_at TEXT, raw_response_sha256 TEXT);
            """)
        self.recover_orphans()

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "queue.sqlite3", timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def audio_path(self, chunk_id):
        return self.root / "chunks" / chunk_id / (chunk_id + ".m4a")

    def raw_path(self, chunk_id):
        return self.root / "raw" / (chunk_id + ".json")

    def publish_run(self, chunk_id):
        with self.db() as db:
            context = db.execute("SELECT * FROM run_context WHERE chunk_id=? ORDER BY charge_id DESC LIMIT 1",
                                 (chunk_id,)).fetchone()
            if context is None:
                return  # Old responses have no verified requested-model or request-start record.
            context = dict(context)
            if context["completed_at"] is None:
                raw_path = self.raw_path(chunk_id)
                context["raw_response_sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
                context["completed_at"] = dt.datetime.fromtimestamp(raw_path.stat().st_mtime, dt.timezone.utc).isoformat()
                db.execute("UPDATE run_context SET completed_at=?,raw_response_sha256=? WHERE charge_id=?",
                           (context["completed_at"], context["raw_response_sha256"], context["charge_id"]))
        directory = self.root / "runs" / context["run_id"]
        directory.mkdir(exist_ok=True, mode=0o700)
        fsync_dir(directory.parent)
        snapshot = directory / "response.json"
        # The per-run snapshot survives later changes to the compatibility raw/ cache.
        response = (snapshot if snapshot.exists() else self.raw_path(chunk_id)).read_bytes()
        if hashlib.sha256(response).hexdigest() != context["raw_response_sha256"]:
            raise ValueError("saved response failed processing-run integrity check")
        atomic_write_once(snapshot, response)
        record = {name: context[name] for name in (
            "run_id", "chunk_id", "input_sha256", "provider", "model", "started_at", "completed_at", "raw_response_sha256")}
        record["schema_version"] = 1
        record["raw_response_path"] = snapshot.relative_to(self.root).as_posix()
        atomic_write_once(directory / "run.json", json.dumps(record, ensure_ascii=False, indent=2))

    def insert(self, db, meta, actual):
        db.execute("INSERT INTO chunks(chunk_id,session_id,sequence,metadata,local_date,duration) VALUES(?,?,?,?,?,?)",
                   (meta["chunk_id"], meta["session_id"], meta["sequence"], canonical(meta), validate_metadata(meta), actual))

    def recover_orphans(self):
        # A crash between durable-directory rename and SQLite commit is safe to replay.
        with self.lock, self.db() as db:
            for folder in sorted((self.root / "chunks").iterdir()):
                if not folder.is_dir() or db.execute("SELECT 1 FROM chunks WHERE chunk_id=?", (folder.name,)).fetchone():
                    continue
                try:
                    meta = json.loads((folder / "metadata.json").read_text())
                    validate_metadata(meta)
                    if meta["chunk_id"] != folder.name:
                        continue
                    audio = self.audio_path(meta["chunk_id"])
                    if audio.stat().st_size > MAX_BYTES or hashlib.sha256(audio.read_bytes()).hexdigest() != meta["sha256"]:
                        continue
                    actual = probe_audio(audio, meta["duration_seconds"])
                    self.insert(db, meta, actual)
                except (OSError, ValueError, Rejected, sqlite3.IntegrityError):
                    continue

    def accept(self, meta, incoming):
        validate_metadata(meta)
        chunk_id = meta["chunk_id"]
        if incoming.stat().st_size > MAX_BYTES:
            raise Rejected(413, "audio exceeds 24000000 bytes")
        if hashlib.sha256(incoming.read_bytes()).hexdigest() != meta["sha256"]:
            raise Rejected(422, "sha256 mismatch")
        actual = probe_audio(incoming, meta["duration_seconds"])
        with self.lock, self.db() as db:
            # Cross-process writers serialize before filesystem publication.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT metadata FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
            if row:
                if row["metadata"] != canonical(meta):
                    raise Rejected(409, "chunk_id already exists with different content or metadata")
                stored_audio = self.audio_path(chunk_id)
                if not stored_audio.is_file() or hashlib.sha256(stored_audio.read_bytes()).hexdigest() != meta["sha256"]:
                    raise Rejected(503, "stored audio failed integrity check; no acknowledgement")
                return False
            if db.execute("SELECT 1 FROM chunks WHERE session_id=? AND sequence=?", (meta["session_id"], meta["sequence"])).fetchone():
                raise Rejected(409, "session sequence already exists")
            folder = self.root / "chunks" / chunk_id
            if folder.exists():
                raise Rejected(503, "unrecovered chunk directory; operator review required")
            staging = Path(tempfile.mkdtemp(prefix="chunk-", dir=self.root / "tmp"))
            try:
                os.replace(incoming, staging / meta["filename"])
                atomic_write(staging / "metadata.json", canonical(meta))
                fsync_dir(staging)
                os.replace(staging, folder)
                fsync_dir(folder.parent)
                self.insert(db, meta, actual)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return True

    def recover_worker(self):
        with self.db() as db:
            for row in db.execute("SELECT chunk_id FROM chunks WHERE status='processing'").fetchall():
                state = "raw_ready" if self.raw_path(row["chunk_id"]).exists() else "needs_review"
                db.execute("UPDATE chunks SET status=?,last_error=? WHERE chunk_id=?",
                           (state, "interrupted request; inspect before retrying to avoid duplicate charges", row["chunk_id"]))

    def rows(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM chunks")]

    def set_state(self, chunk_id, state, error=None, retry_at=0):
        with self.db() as db:
            db.execute("UPDATE chunks SET status=?,last_error=?,next_attempt=? WHERE chunk_id=?", (state, error, retry_at, chunk_id))

    def status(self):
        with self.db() as db:
            states = {r["status"]: r["n"] for r in db.execute("SELECT status,count(*) n FROM chunks GROUP BY status")}
            amounts = {r["month"]: round(r["total"], 6) for r in db.execute("SELECT month,sum(estimate) total FROM charges WHERE outcome!='not_processed' GROUP BY month")}
            attention = [dict(r) for r in db.execute("SELECT chunk_id,status,last_error FROM chunks WHERE status IN ('needs_review','failed','budget_blocked','derive_failed')")]
        return {"states": states, "estimated_reserved_usd_by_month": amounts, "attention": attention}


class ReceiverServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Bad/abandoned TLS handshakes are isolated to one worker thread.
        pass


def handler_for(store, token):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def setup(self):
            super().setup()
            self.connection.settimeout(30)
            if isinstance(self.connection, ssl.SSLSocket):
                self.connection.do_handshake()

        def log_message(self, *_):
            pass  # No request paths, metadata, audio or credentials in access logs.

        def reply(self, status, body):
            encoded = canonical(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)
            self.close_connection = True

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, {"ok": True}); return
            if self.path == "/v1/status":
                try:
                    auth = self.headers.get("Authorization", "")
                    if not hmac.compare_digest(auth.encode(), ("Bearer " + token).encode()):
                        raise Rejected(401, "unauthorized")
                    self.reply(200, read_status(store))
                except Rejected as exc:
                    self.reply(exc.status, {"error": exc.message})
                except (OSError, sqlite3.Error):
                    with contextlib.suppress(OSError):
                        self.reply(503, {"error": "status unavailable"})
                return
            self.reply(404, {"ok": False})

        def do_POST(self):
            temp = None
            try:
                auth = self.headers.get("Authorization", "")
                if not hmac.compare_digest(auth.encode(), ("Bearer " + token).encode()):
                    raise Rejected(401, "unauthorized")
                if self.path != "/v1/chunks":
                    raise Rejected(404, "not found")
                if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                    raise Rejected(411, "one Content-Length header is required")
                try:
                    length = int(self.headers["Content-Length"])
                    if length <= 0:
                        raise ValueError()
                except (TypeError, ValueError):
                    raise Rejected(400, "invalid Content-Length")
                if length > MAX_BYTES:
                    raise Rejected(413, "audio exceeds 24000000 bytes")
                try:
                    encoded = self.headers.get("X-Chunk-Metadata", "")
                    if len(encoded) > 8192:
                        raise ValueError()
                    meta = json.loads(base64.b64decode(encoded, validate=True))
                except (ValueError, UnicodeError):
                    raise Rejected(400, "invalid metadata encoding")
                validate_metadata(meta)
                fd, filename = tempfile.mkstemp(prefix="upload-", dir=store.root / "tmp")
                temp = Path(filename)
                with os.fdopen(fd, "wb") as output:
                    remaining = length
                    while remaining:
                        block = self.rfile.read(min(65536, remaining))
                        if not block:
                            raise Rejected(400, "incomplete audio body")
                        output.write(block)
                        remaining -= len(block)
                    output.flush()
                    os.fsync(output.fileno())
                created = store.accept(meta, temp)
                self.reply(201 if created else 200, {"chunk_id": meta["chunk_id"], "sha256": meta["sha256"], "stored": True})
            except Rejected as exc:
                self.reply(exc.status, {"error": exc.message})
            except (TimeoutError, socket.timeout):
                with contextlib.suppress(OSError):
                    self.reply(408, {"error": "upload timed out"})
            except (OSError, sqlite3.Error):
                with contextlib.suppress(OSError):
                    self.reply(503, {"error": "storage unavailable; no acknowledgement"})
            finally:
                if temp:
                    temp.unlink(missing_ok=True)
    return Handler


def load_config(path):
    config_path = Path(path).expanduser().resolve()
    config = json.loads(config_path.read_text())
    if config.get("provider") not in PROVIDERS:
        raise ValueError("provider must explicitly be groq, siliconflow or openai")
    for field in ("price_per_hour_usd", "monthly_budget_usd"):
        value = config.get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{field} must be an explicit nonnegative number")
    if not isinstance(config.get("key_file"), str):
        raise ValueError("key_file must be specified")
    key_path = Path(config["key_file"]).expanduser()
    config["key_file"] = str(key_path if key_path.is_absolute() else config_path.parent / key_path)
    config.setdefault("language", "zh")
    return config


def upload_cloud(audio_path, config):
    endpoint, model = PROVIDERS[config["provider"]]
    boundary = "hemory-" + uuid.uuid4().hex
    parts = []
    fields = {"model": model}
    if config["provider"] != "siliconflow":
        fields["response_format"] = "json"
    if config["provider"] != "siliconflow" and config.get("language"):
        fields["language"] = config["language"]
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.m4a"\r\nContent-Type: audio/mp4\r\n\r\n'.encode())
    parts.append(audio_path.read_bytes())
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    request = urllib.request.Request(endpoint, data=b"".join(parts), headers={
        "Authorization": "Bearer " + read_secret(config["key_file"]),
        "Content-Type": "multipart/form-data; boundary=" + boundary,
    }, method="POST")
    # No automatic retries or redirects carrying credentials to another origin.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=180) as response:
        body = response.read(8_000_001)
        if len(body) > 8_000_000:
            raise ValueError("response exceeds limit")
        return json.loads(body)


def transcript_text(raw):
    if isinstance(raw, dict) and isinstance(raw.get("text"), str):
        return raw["text"].strip()
    if isinstance(raw, dict) and isinstance(raw.get("segments"), list):
        if all(isinstance(s, dict) and isinstance(s.get("text"), str) for s in raw["segments"]):
            return "\n".join(s["text"].strip() for s in raw["segments"]).strip()
    raise ValueError("cloud response has no supported transcript field")


def read_status(store, limit=500):
    """Read-only chunk list with transcript state and text. Never triggers STT.

    Prefers the local semantic (MLX Whisper) transcript and state when present,
    falling back to the legacy cloud transcript. A malformed transcript must not
    hide the whole list; it degrades that one row to text=None.
    """
    semantic = _read_semantic_analyses(store.root)
    result = []
    for row in store.rows():
        try:
            meta = json.loads(row["metadata"])
            started = meta.get("started_at", "")
        except (ValueError, KeyError, TypeError):
            started = ""
        text = None
        status = row["status"]
        local = semantic.get(row["chunk_id"])
        if local is not None:
            text = local["text"]
            status = {"succeeded": "done", "failed": "failed", "needs_review": "needs_review",
                      "running": "processing", "pending": "pending"}.get(local["state"], row["status"])
        else:
            try:
                raw_path = store.raw_path(row["chunk_id"])
                if raw_path.exists():
                    text = transcript_text(json.loads(raw_path.read_text()))
            except (ValueError, OSError, KeyError):
                text = None
        result.append({
            "chunk_id": row["chunk_id"],
            "started_at": started,
            "duration": row["duration"],
            "status": status,
            "text": text,
        })
    result.sort(key=lambda c: (c["started_at"], c["chunk_id"]), reverse=True)
    return {"ok": True, "chunks": result[:limit]}


def _read_semantic_analyses(root):
    """Map chunk_id -> {state, text} from the local semantic analyses table."""
    path = root / "semantic" / "index.sqlite3"
    if not path.exists():
        return {}
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            out = {}
            for row in db.execute("SELECT chunk_id, state, result FROM analyses"):
                text = None
                if row["result"]:
                    try:
                        parsed = json.loads(row["result"])
                        text = "\n".join(u.get("text", "") for u in parsed.get("utterances", []) if u.get("text")).strip() or None
                    except (ValueError, KeyError, TypeError):
                        text = None
                out[row["chunk_id"]] = {"state": row["state"], "text": text}
            return out
    except sqlite3.Error:
        return {}


def chunk_markdown(store, row, relative_to):
    meta = json.loads(row["metadata"])
    text = transcript_text(json.loads(store.raw_path(row["chunk_id"]).read_text()))
    audio_link = os.path.relpath(store.audio_path(row["chunk_id"]), relative_to).replace(os.sep, "/")
    started = dt.datetime.fromisoformat(meta["started_at"]).astimezone(LOCAL_TZ)
    ended = started + dt.timedelta(seconds=row["duration"])
    return (f"## {started:%H:%M:%S}–{ended:%H:%M:%S} · 片段 {meta['sequence']}\n\n"
            f"- 会话：`{meta['session_id']}`\n- 片段：`{meta['chunk_id']}`\n"
            f"- 开始：{started.isoformat()}（Asia/Shanghai）\n"
            f"- 实测时长：{row['duration']:.3f} 秒\n- 转写服务：{row['provider']}\n"
            f"- [原始录音]({audio_link})\n- 说话人：未识别；转写未自动核实\n\n"
            f"{text if text else '（云端返回空转写；保留录音供核对）'}\n")


def derive_markdown(store, row):
    day = row["local_date"]
    dest = store.root / "transcripts" / day / (row["chunk_id"] + ".md")
    atomic_write(dest, f"# {day} 对话记录\n\n" + chunk_markdown(store, row, dest.parent))
    # Aggregate deterministically by session start, sequence and UUID, independent of arrival order.
    rows = [r for r in store.rows() if r["local_date"] == day and (r["status"] == "done" or r["chunk_id"] == row["chunk_id"])]
    starts = {}
    for item in rows:
        started = dt.datetime.fromisoformat(json.loads(item["metadata"])["started_at"]).timestamp()
        starts[item["session_id"]] = min(starts.get(item["session_id"], started), started)
    rows.sort(key=lambda r: (starts[r["session_id"]], r["session_id"], r["sequence"], r["chunk_id"]))
    daily = store.root / "daily" / (day + ".md")
    content = f"# {day} 对话记录\n\n时区：Asia/Shanghai。仅包含已转写片段；原始录音为核对依据。\n\n"
    content += "\n".join(chunk_markdown(store, item, daily.parent) for item in rows)
    atomic_write(daily, content)
    store.set_state(row["chunk_id"], "done")


def process_one(store, config, cloud=upload_cloud):
    # Local migration gate: finish the current request, then stop claiming another.
    # This does not change receiver behavior or the seven-field v1 upload contract.
    if (store.root / 'private' / 'pause-legacy-worker').exists():
        return False
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM chunks WHERE status='raw_ready' ORDER BY local_date,session_id,sequence LIMIT 1").fetchone()
        if row is None:
            row = db.execute("SELECT * FROM chunks WHERE status='pending' AND next_attempt<=? ORDER BY local_date,session_id,sequence LIMIT 1", (time.time(),)).fetchone()
        if row is None:
            return False
        row = dict(row)
        if store.raw_path(row["chunk_id"]).exists():
            db.execute("UPDATE chunks SET status='raw_ready' WHERE chunk_id=?", (row["chunk_id"],))
            charge_id = None
        else:
            try:
                expected_hash = json.loads(row["metadata"])["sha256"]
                if hashlib.sha256(store.audio_path(row["chunk_id"]).read_bytes()).hexdigest() != expected_hash:
                    raise ValueError()
            except (OSError, ValueError):
                db.execute("UPDATE chunks SET status='failed',last_error='stored audio failed integrity check; restore original before retry' WHERE chunk_id=?", (row["chunk_id"],))
                return True
            if row["attempts"] >= 3:
                db.execute("UPDATE chunks SET status='needs_review',last_error='automatic attempt limit reached' WHERE chunk_id=?", (row["chunk_id"],))
                return True
            month = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m")
            estimate = max(row["duration"], 10) / 3600 * config["price_per_hour_usd"]
            reserved = db.execute("SELECT coalesce(sum(estimate),0) FROM charges WHERE month=? AND outcome!='not_processed'", (month,)).fetchone()[0]
            if reserved + estimate > config["monthly_budget_usd"] + 1e-9:
                db.execute("UPDATE chunks SET status='budget_blocked',last_error='monthly estimated budget reached' WHERE chunk_id=?", (row["chunk_id"],))
                return True
            charge_id = db.execute("INSERT INTO charges(chunk_id,month,estimate,provider,outcome,created_at) VALUES(?,?,?,?,?,?)",
                                   (row["chunk_id"], month, estimate, config["provider"], "reserved", time.time())).lastrowid
            db.execute("INSERT INTO run_context(charge_id,run_id,chunk_id,input_sha256,provider,model,started_at) VALUES(?,?,?,?,?,?,?)",
                       (charge_id, str(uuid.uuid4()), row["chunk_id"], expected_hash, config["provider"],
                        PROVIDERS[config["provider"]][1], dt.datetime.now(dt.timezone.utc).isoformat()))
            db.execute("UPDATE chunks SET status='processing',attempts=attempts+1,provider=? WHERE chunk_id=?", (config["provider"], row["chunk_id"]))
            row["provider"] = config["provider"]
    if charge_id is not None:
        try:
            raw = cloud(store.audio_path(row["chunk_id"]), config)
            atomic_write(store.raw_path(row["chunk_id"]), json.dumps(raw, ensure_ascii=False, indent=2))
            store.set_state(row["chunk_id"], "raw_ready")
            with store.db() as db:
                db.execute("UPDATE charges SET outcome='response_saved' WHERE id=?", (charge_id,))
        except urllib.error.HTTPError as exc:
            exc.close()
            # Error bodies may contain provider-sensitive details; keep only the status code.
            if exc.code == 429:
                store.set_state(row["chunk_id"], "pending", "HTTP 429; deferred retry", time.time() + min(3600, 60 * 2 ** row["attempts"]))
                with store.db() as db:
                    db.execute("UPDATE charges SET outcome='not_processed' WHERE id=?", (charge_id,))
            elif 400 <= exc.code < 500:
                store.set_state(row["chunk_id"], "failed", f"HTTP {exc.code}; inspect configuration before retry")
            else:
                store.set_state(row["chunk_id"], "needs_review", f"HTTP {exc.code}; processing uncertain, no automatic retry")
            return True
        except Exception:
            store.set_state(row["chunk_id"], "needs_review", "request or persistence failed; processing uncertain, no automatic retry")
            return True
    try:
        store.publish_run(row["chunk_id"])
        derive_markdown(store, row)
    except Exception:
        store.set_state(row["chunk_id"], "derive_failed", "raw response retained; processing record or Markdown derivation failed")
    return True


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("server")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--cert", type=Path, required=True)
    server.add_argument("--key", type=Path, required=True)
    server.add_argument("--token-file", type=Path, required=True)
    worker = commands.add_parser("worker")
    worker.add_argument("--config", type=Path, required=True)
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--dry-run", action="store_true")
    commands.add_parser("status")
    retry = commands.add_parser("retry", help="Explicit retry may incur another cloud charge")
    retry.add_argument("chunk_id")
    args = parser.parse_args()
    store = Store(args.data_dir)
    if args.command == "server":
        token = read_secret(args.token_file)
        if len(token) < 32:
            raise ValueError("receiver token must be at least 32 characters")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert, args.key)
        httpd = ReceiverServer((args.host, args.port), handler_for(store, token))
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
        print(f"HTTPS receiver listening on {args.host}:{args.port}", flush=True)
        httpd.serve_forever()
    elif args.command == "status":
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
    elif args.command == "retry":
        if str(uuid.UUID(args.chunk_id)) != args.chunk_id:
            raise ValueError("canonical UUID required")
        with store.db() as db:
            row = db.execute("SELECT status FROM chunks WHERE chunk_id=?", (args.chunk_id,)).fetchone()
            if row is None or row["status"] not in ("needs_review", "failed", "budget_blocked", "derive_failed"):
                raise ValueError("chunk is not awaiting retry")
            state = "raw_ready" if store.raw_path(args.chunk_id).exists() else "pending"
            db.execute("UPDATE chunks SET status=?,attempts=0,next_attempt=0,last_error=NULL WHERE chunk_id=?", (state, args.chunk_id))
        print("Retry queued. If the earlier request was processed, another cloud request may incur another charge.")
    elif args.command == "worker":
        config = load_config(args.config)
        if args.dry_run:
            print(json.dumps({"dry_run": True, "provider": config["provider"], "cloud_requests": 0, "status": store.status()}, ensure_ascii=False, indent=2))
            return
        # Validate the local secret before reserving or claiming any audio.
        read_secret(config["key_file"])
        with (store.root / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit("Another worker is already active")
            store.recover_worker()
            while True:
                worked = process_one(store, config)
                if args.once:
                    break
                time.sleep(1 if worked else 15)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        # Never echo credential values or provider response bodies.
        raise SystemExit(f"Configuration or filesystem error: {type(error).__name__}; check paths and private configuration")
