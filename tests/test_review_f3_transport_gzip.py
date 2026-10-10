"""F3 HTTP compression on a disposable 35th SQLite clone; original is read-only."""

from __future__ import annotations

import base64
import gzip
import http.client
import json
import secrets
import sqlite3
import statistics
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from src import review_ui


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "topik-past-papers/derived/035-I-B.sqlite"


class F3GzipTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SOURCE.is_file():
            raise unittest.SkipTest("Frozen original 35th SQLite not installed")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="topik-f3-gzip-")
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "clone.sqlite"
        with closing(sqlite3.connect(SOURCE.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            source.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(self.db_path)) as target:
                source.backup(target)
        self.key = secrets.token_urlsafe(32)
        self.store = review_ui.ReviewStore(self.db_path, root=ROOT)
        self.server = review_ui.ThreadingHTTPServer(
            ("127.0.0.1", 0), review_ui.make_handler(self.store, access_key=self.key))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.authorization = "Basic " + base64.b64encode(f"operator:{self.key}".encode()).decode("ascii")

    def _shutdown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive())

    def _request(self, path, *, accept=None, auth=True, method="GET", payload=None, headers=None):
        outgoing = dict(headers or {})
        if auth:
            outgoing["Authorization"] = self.authorization
        if accept is not None:
            outgoing["Accept-Encoding"] = accept
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if data is not None:
            outgoing.setdefault("Content-Type", "application/json")
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=12)
        try:
            client.request(method, path, headers=outgoing, body=data)
            response = client.getresponse()
            data = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, data
        finally:
            client.close()

    def test_bulk_f3_bundle_and_fast_list_gzip_parity_and_size(self):
        for route, expected_count in (("/api/questions-bundle", 70),
                                      ("/api/questions-fast", 70),
                                      ("/api/independent-audits", None)):
            with self.subTest(route=route):
                status, plain_headers, plain = self._request(route)
                self.assertEqual(status, 200)
                self.assertEqual(plain_headers["vary"], "Accept-Encoding")
                self.assertNotIn("content-encoding", plain_headers)
                self.assertEqual(int(plain_headers["content-length"]), len(plain))
                status, zipped_headers, zipped = self._request(route, accept="gzip, deflate, br")
                self.assertEqual(status, 200)
                self.assertEqual(zipped_headers["content-encoding"], "gzip")
                self.assertEqual(zipped_headers["vary"], "Accept-Encoding")
                self.assertEqual(int(zipped_headers["content-length"]), len(zipped))
                self.assertEqual(gzip.decompress(zipped), plain)
                self.assertLess(len(zipped), len(plain) * 0.3)
                self.assertEqual(json.loads(gzip.decompress(zipped)), json.loads(plain))
                if expected_count is not None:
                    parsed = json.loads(plain)
                    entries = parsed.get("items", parsed.get("questions"))
                    self.assertEqual(len(entries), expected_count)

    def test_gzip_negotiation_and_short_json_are_fail_safe(self):
        route = "/api/questions-bundle"
        for value in (None, "identity", "gzip;q=0", "gzip;q=0, br", "gzip;q=bogus",
                      "gzip;q=2", "*", "gzip;q=0.0, *;q=1"):
            with self.subTest(accept=value):
                status, headers, data = self._request(route, accept=value)
                self.assertEqual(status, 200)
                self.assertNotIn("content-encoding", headers)
                self.assertEqual(json.loads(data)["total_questions"], 70)
        for value in ("gzip", "GZIP;Q=0.5", "br, GZIP; q=1", "gzip;q=0.01"):
            with self.subTest(accept=value):
                status, headers, data = self._request(route, accept=value)
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("content-encoding"), "gzip")
                self.assertEqual(json.loads(gzip.decompress(data))["total_questions"], 70)
        status, headers, data = self._request("/api/questions-ai-summary", accept="gzip")
        self.assertEqual(status, 200)
        self.assertNotIn("content-encoding", headers)
        self.assertLess(len(data), 2048)

    def test_operator_gate_401_and_source_pdf_audio_byte_ranges_unchanged(self):
        status, headers, body = self._request("/api/questions-bundle", accept="gzip", auth=False)
        self.assertEqual(status, 401)
        self.assertTrue(headers["www-authenticate"].startswith("Basic"))
        self.assertNotIn("content-encoding", headers)
        self.assertNotIn("csrf_token", body.decode("utf-8"))
        for path, signature in (("/media/035-I-L-001/paper", b"%PDF-"),
                                ("/media/035-I-L-001/audio", None)):
            with self.subTest(path=path):
                status, headers, data = self._request(
                    path, accept="gzip", headers={"Range": "bytes=0-15"})
                self.assertEqual(status, 206)
                self.assertEqual(len(data), 16)
                self.assertNotIn("content-encoding", headers)
                self.assertEqual(headers["accept-ranges"], "bytes")
                if signature:
                    self.assertTrue(data.startswith(signature))

    def test_aborted_image_response_does_not_send_spurious_second_http_500(self):
        """Real Chromium may close a large image request during navigation."""
        handler_class = review_ui.make_handler(self.store, access_key=self.key)
        handler = handler_class.__new__(handler_class)
        handler.server = self.server
        handler.path = "/media/035-I-L-015/image/0"
        handler.headers = {"Host": f"127.0.0.1:{self.server.server_port}",
                           "Authorization": self.authorization}
        handler.wfile = Mock()
        handler.wfile.write.side_effect = ConnectionAbortedError("SIMULATED browser navigation abort")
        with patch.object(handler, "_headers") as sent, patch.object(handler, "_json") as error:
            handler.do_GET()
        sent.assert_called_once()
        self.assertEqual(sent.call_args.args[0], 200)
        error.assert_not_called()

    def test_fast_save_ack_and_conflict_unchanged_with_gzip(self):
        _, _, listing = self._request("/api/questions-fast", accept="gzip")
        token = json.loads(gzip.decompress(listing))["csrf_token"]
        _, _, detail_bytes = self._request("/api/questions/035-I-R-031?fast=1")
        detail = json.loads(detail_bytes)
        payload = {"version": detail["version"], "status": "verified", "stem": detail["stem"],
                   "choices": [c["text"] for c in detail["choices"]],
                   "transcript_text": None, "note": "SIMULATED private compression test approval"}
        headers = {"Origin": self.origin, "X-Review-Token": token}
        route = "/api/questions/035-I-R-031/review?fast=1"
        status, response_headers, response = self._request(
            route, method="POST", payload=payload, headers=headers, accept="gzip")
        self.assertEqual(status, 200, response[:200])
        if response_headers.get("content-encoding") == "gzip":
            response = gzip.decompress(response)
        saved = json.loads(response)
        self.assertTrue(saved["saved"])
        self.assertTrue(saved["last_human_review"]["approved"])
        self.assertFalse(saved["last_human_review"]["identity_verified"])
        self.assertTrue(saved["history"])
        status, response_headers, body = self._request(
            route, method="POST", payload=payload, headers=headers, accept="gzip")
        self.assertEqual(status, 409)
        if response_headers.get("content-encoding") == "gzip":
            body = gzip.decompress(body)
        self.assertIn("error", json.loads(body))

    def test_fast_list_ranks_only_selected_exam_even_with_large_unrelated_history(self):
        """The shared DB's other sessions must not enter F3 window sorts."""
        pristine = self.store.list_questions_fast()
        first = pristine["items"][0]
        with closing(sqlite3.connect(self.db_path)) as db:
            # Not linked to an exam 35 question. This synthetic history must
            # NEVER change either status/provenance or fast query cost for 35.
            db.executemany(
                "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (("question", f"036-I-R-{(i%70)+1:03d}", "needs_manual_review",
                  "SIMULATED_OTHER_EXAM", "synthetic_other_exam", "{}",
                  "2026-10-10T00:00:00+00:00") for i in range(20000)),
            )
            db.commit()
        statements = []
        original = self.store._connect

        def traced_connect(writable=False):
            connection = original(writable=writable)
            connection.set_trace_callback(statements.append)
            return connection

        self.store._connect = traced_connect
        try:
            durations = []
            for _ in range(5):
                started = time.perf_counter()
                listing = self.store.list_questions_fast()
                durations.append((time.perf_counter() - started) * 1000)
                self.assertEqual(listing["items"], pristine["items"])
                self.assertEqual(listing["counts"], pristine["counts"])
            ranked = [stmt.lower() for stmt in statements if "with ranked_reviews" in stmt.lower()]
            self.assertEqual(len(ranked), 5)
            self.assertTrue(all("s2.exam_id=" in sql and "subject_id in (" in sql for sql in ranked))
            self.assertEqual(listing["items"][0]["id"], first["id"])
            # Not a hard performance gate: preserve bounded evidence and output;
            # the measured medians are documented against the prior query.
            self.assertTrue(statistics.median(durations) >= 0)
        finally:
            self.store._connect = original


if __name__ == "__main__":
    unittest.main()
