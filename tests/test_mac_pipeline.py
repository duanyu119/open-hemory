"""Local integration tests: generated audio + real HTTPS; cloud responses are stubs."""
import base64
import datetime as dt
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import ssl
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import uuid

MODULE = Path(__file__).resolve().parents[1] / "mac/hemory_local.py"
spec = importlib.util.spec_from_file_location("hemory_local", MODULE)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        path = Path(cls.fixture.name)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ar", "16000", "-ac", "1", "-c:a", "aac", str(path / "sample.m4a")], check=True)
        cls.audio = (path / "sample.m4a").read_bytes()
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(path / "key.pem"), "-out", str(path / "cert.pem"), "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
        cls.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.tls.load_cert_chain(path / "cert.pem", path / "key.pem")

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = h.Store(self.temp.name)
        self.token = 'fixture-receiver-token-not-for-production-0001'
        self.server = h.ReceiverServer(("127.0.0.1", 0), h.handler_for(self.store, self.token))
        self.server.socket = self.tls.wrap_socket(self.server.socket, server_side=True, do_handshake_on_connect=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = {"provider": "groq", "price_per_hour_usd": 0.04, "monthly_budget_usd": 1}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def metadata(self, sequence=0, session=None):
        chunk_id = str(uuid.uuid4())
        return {"chunk_id": chunk_id, "session_id": session or str(uuid.uuid4()), "started_at": f"2026-09-17T08:00:{sequence:02d}+08:00", "duration_seconds": 1.0, "sequence": sequence,
                "sha256": hashlib.sha256(self.audio).hexdigest(), "filename": chunk_id + ".m4a"}

    def post(self, meta, token=None, length=None):
        connection = http.client.HTTPSConnection("127.0.0.1", self.server.server_port, context=ssl._create_unverified_context(), timeout=5)
        headers = {"Authorization": "Bearer " + (self.token if token is None else token), "X-Chunk-Metadata": base64.b64encode(json.dumps(meta).encode()).decode(), "Content-Length": str(len(self.audio) if length is None else length)}
        connection.request("POST", "/v1/chunks", self.audio, headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def get(self, path, token=None):
        connection = http.client.HTTPSConnection("127.0.0.1", self.server.server_port, context=ssl._create_unverified_context(), timeout=5)
        headers = {} if token is None else {"Authorization": "Bearer " + token}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_status_endpoint_requires_auth_and_returns_chunk_text(self):
        self.assertEqual(self.get("/v1/status")[0], 401)
        self.assertEqual(self.get("/v1/status", token="wrong-token-00000000000000000000000000")[0], 401)
        meta = self.metadata(0)
        self.assertEqual(self.post(meta)[0], 201)
        status, body = self.get("/v1/status", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        chunk = next(c for c in body["chunks"] if c["chunk_id"] == meta["chunk_id"])
        self.assertEqual(chunk["status"], "pending")
        self.assertIsNone(chunk["text"])
        (self.store.root / "raw").mkdir(exist_ok=True)
        (self.store.root / "raw" / (meta["chunk_id"] + ".json")).write_text(json.dumps({"text": "转写文字"}))
        with self.store.db() as db:
            db.execute("UPDATE chunks SET status='done' WHERE chunk_id=?", (meta["chunk_id"],))
        status, body = self.get("/v1/status", token=self.token)
        chunk = next(c for c in body["chunks"] if c["chunk_id"] == meta["chunk_id"])
        self.assertEqual(chunk["status"], "done")
        self.assertEqual(chunk["text"], "转写文字")

    def test_authenticated_durable_upload_duplicate_and_conflicts(self):
        meta = self.metadata()
        self.assertEqual(self.post(meta, "invalid")[0], 401)
        self.assertEqual(self.store.rows(), [])
        status, ack = self.post(meta)
        self.assertEqual(status, 201)
        self.assertEqual(ack, {"chunk_id": meta["chunk_id"], "sha256": meta["sha256"], "stored": True})
        self.assertEqual(self.store.audio_path(meta["chunk_id"]).read_bytes(), self.audio)
        self.assertEqual(self.post(meta)[0], 200)
        self.assertEqual(self.post({**meta, "started_at": "2026-09-17T09:00:00+08:00"})[0], 409)
        self.assertEqual(self.post(self.metadata(session=meta["session_id"]))[0], 409)
        self.assertEqual(len(self.store.rows()), 1)

    def test_stalled_tls_client_does_not_block_other_connections(self):
        stalled = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        try:
            connection = http.client.HTTPSConnection("127.0.0.1", self.server.server_port, context=ssl._create_unverified_context(), timeout=2)
            connection.request("GET", "/health")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"ok": True})
            connection.close()
        finally:
            stalled.close()

    def test_corrupt_stored_audio_never_receives_ack_or_cloud_request(self):
        meta = self.metadata()
        self.assertEqual(self.post(meta)[0], 201)
        self.store.audio_path(meta["chunk_id"]).write_bytes(b"truncated")
        status, result = self.post(meta)
        self.assertEqual(status, 503)
        self.assertNotIn("stored", result)
        h.process_one(self.store, self.config, cloud=lambda *_: self.fail("corrupt audio must not be uploaded"))
        self.assertEqual(self.store.rows()[0]["status"], "failed")

    def test_invalid_metadata_hash_duration_and_size_are_rejected(self):
        meta = self.metadata()
        self.assertEqual(self.post({**meta, "chunk_id": "../../escape"})[0], 400)
        self.assertEqual(self.post({**meta, "sha256": "0" * 64})[0], 422)
        self.assertEqual(self.post({**meta, "duration_seconds": 80})[0], 422)
        self.assertEqual(self.post({**meta, "started_at": "2026-09-17T08:00:00"})[0], 400)
        self.assertEqual(self.post(meta, length=h.MAX_BYTES + 1)[0], 413)
        self.assertEqual(self.store.rows(), [])

    def test_storage_recovers_committed_audio_if_database_commit_was_interrupted(self):
        meta = self.metadata()
        self.assertEqual(self.post(meta)[0], 201)
        with self.store.db() as db:
            db.execute("DELETE FROM chunks")
        recovered = h.Store(self.temp.name)
        self.assertEqual(len(recovered.rows()), 1)
        self.assertEqual(recovered.rows()[0]["status"], "pending")
        self.assertEqual(self.post(meta)[0], 200)

    def test_rate_limit_retains_audio_and_retry_produces_raw_and_markdown(self):
        meta = self.metadata()
        self.post(meta)
        def limited(*_):
            raise urllib.error.HTTPError("https://example.invalid", 429, "limited", {}, None)
        self.assertTrue(h.process_one(self.store, self.config, cloud=limited))
        row = self.store.rows()[0]
        self.assertEqual(row["status"], "pending")
        self.assertGreater(row["next_attempt"], time.time())
        self.assertTrue(self.store.audio_path(meta["chunk_id"]).exists())
        self.assertFalse(h.process_one(self.store, self.config, cloud=lambda *_: self.fail("must honor backoff")))
        with self.store.db() as db:
            db.execute("UPDATE chunks SET next_attempt=0")
        self.assertTrue(h.process_one(self.store, self.config, cloud=lambda *_: {"text": "这是一段本机测试。"}))
        self.assertEqual(self.store.rows()[0]["status"], "done")
        self.assertEqual(json.loads(self.store.raw_path(meta["chunk_id"]).read_text())["text"], "这是一段本机测试。")
        daily = (self.store.root / "daily/2026-09-17.md").read_text()
        self.assertIn("这是一段本机测试。", daily)
        self.assertIn("说话人：未识别", daily)
        self.assertIn("../chunks/", daily)
        self.assertEqual(len(list((self.store.root / "transcripts/2026-09-17").glob("*.md"))), 1)

    def test_uncertain_cloud_failure_stops_automatic_charging(self):
        meta = self.metadata()
        self.post(meta)
        def timeout(*_):
            raise TimeoutError()
        self.assertTrue(h.process_one(self.store, self.config, cloud=timeout))
        self.assertEqual(self.store.rows()[0]["status"], "needs_review")
        self.assertFalse(h.process_one(self.store, self.config, cloud=lambda *_: self.fail("must not retry uncertain work")))
        self.assertTrue(self.store.audio_path(meta["chunk_id"]).exists())
        self.assertGreater(self.store.status()["estimated_reserved_usd_by_month"][dt.datetime.now(h.LOCAL_TZ).strftime("%Y-%m")], 0)

    def test_raw_saved_before_crash_recovers_without_second_cloud_request(self):
        meta = self.metadata()
        self.post(meta)
        with self.store.db() as db:
            db.execute("UPDATE chunks SET status='processing',provider='groq'")
        h.atomic_write(self.store.raw_path(meta["chunk_id"]), json.dumps({"text": "已保存的转写"}))
        self.store.recover_worker()
        self.assertTrue(h.process_one(self.store, self.config, cloud=lambda *_: self.fail("raw JSON already saved")))
        self.assertEqual(self.store.rows()[0]["status"], "done")
        self.assertEqual(list((self.store.root / "runs").iterdir()), [])  # Never guess a historical model.

    def test_processing_run_snapshot_is_immutable_and_local_replay_does_not_charge(self):
        meta = self.metadata()
        self.post(meta)
        h.process_one(self.store, self.config, cloud=lambda *_: {"text": "可回溯的本机测试。"})
        run_file, = (self.store.root / "runs").glob("*/run.json")
        record = json.loads(run_file.read_text())
        snapshot = self.store.root / record["raw_response_path"]
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["chunk_id"], meta["chunk_id"])
        self.assertEqual(record["input_sha256"], meta["sha256"])
        self.assertEqual(record["model"], "whisper-large-v3-turbo")
        self.assertEqual(record["provider"], "groq")
        self.assertEqual(record["run_id"], run_file.parent.name)
        self.assertEqual(record["raw_response_sha256"], hashlib.sha256(snapshot.read_bytes()).hexdigest())
        self.assertLessEqual(dt.datetime.fromisoformat(record["started_at"]), dt.datetime.fromisoformat(record["completed_at"]))
        originals = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (run_file, snapshot)}
        self.store.set_state(meta["chunk_id"], "raw_ready")
        h.process_one(self.store, self.config, cloud=lambda *_: self.fail("local replay must not call cloud"))
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM charges").fetchone()[0], 1)
        # A future replacement of the compatibility cache cannot erase this response snapshot.
        h.atomic_write(self.store.raw_path(meta["chunk_id"]), '{"text":"later compatibility cache"}')
        self.store.publish_run(meta["chunk_id"])
        for path, original in originals.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), original)
        with self.assertRaises(ValueError):
            h.atomic_write_once(snapshot, b"different historical response")
        self.assertEqual(snapshot.read_bytes(), originals[snapshot][0])

    def test_run_context_recovers_after_response_save_without_using_new_model_or_cloud(self):
        meta = self.metadata()
        self.post(meta)
        with mock.patch.object(self.store, "publish_run", side_effect=SystemExit("simulated process exit")):
            with self.assertRaises(SystemExit):
                h.process_one(self.store, self.config, cloud=lambda *_: {"text": "已落盘，等待恢复。"})
        self.assertEqual(list((self.store.root / "runs").iterdir()), [])
        recovered = h.Store(self.temp.name)
        recovered.recover_worker()
        h.process_one(recovered, {**self.config, "provider": "openai"},
                      cloud=lambda *_: self.fail("saved response must not be billed again"))
        run_file, = (recovered.root / "runs").glob("*/run.json")
        record = json.loads(run_file.read_text())
        self.assertEqual(record["provider"], "groq")
        self.assertEqual(record["model"], "whisper-large-v3-turbo")
        self.assertEqual(recovered.rows()[0]["status"], "done")
        with recovered.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM charges").fetchone()[0], 1)

    def test_budget_blocks_request_and_preserves_audio(self):
        meta = self.metadata()
        self.post(meta)
        config = {**self.config, "monthly_budget_usd": 0}
        self.assertTrue(h.process_one(self.store, config, cloud=lambda *_: self.fail("budget exhausted")))
        self.assertEqual(self.store.rows()[0]["status"], "budget_blocked")
        self.assertTrue(self.store.audio_path(meta["chunk_id"]).exists())

    def test_daily_order_uses_sequence_instead_of_arrival(self):
        session = str(uuid.uuid4())
        second, first = self.metadata(1, session), self.metadata(0, session)
        self.post(second)
        h.process_one(self.store, self.config, cloud=lambda *_: {"text": "第二片"})
        self.post(first)
        h.process_one(self.store, self.config, cloud=lambda *_: {"text": "第一片"})
        daily = (self.store.root / "daily/2026-09-17.md").read_text()
        self.assertLess(daily.index("第一片"), daily.index("第二片"))


if __name__ == "__main__":
    unittest.main()
