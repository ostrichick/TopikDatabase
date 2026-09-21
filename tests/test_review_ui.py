"""Integration tests for the local 35th TOPIK I reviewer.

All writes target a private SQLite backup inside TemporaryDirectory.  The
original ignored corpus database is opened with SQLite mode=ro and is never
passed to ReviewStore for mutation.  No browser, network or production server
is started by these database tests.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import errno
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from src import review_ui


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestReviewStore(unittest.TestCase):
    """Use a distinct SQLite backup for every test; protect the real pilot DB."""

    @classmethod
    def setUpClass(cls):
        if not SOURCE_DB.is_file():
            raise unittest.SkipTest("Ignored 35th TOPIK I pilot SQLite is not installed")

    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="topik-review-ui-test-")
        self.addCleanup(self.sandbox.cleanup)
        self.db_path = Path(self.sandbox.name) / "pilot-copy.sqlite"
        # SQLite backup takes a consistent snapshot, unlike copying a live
        # .sqlite file without a possible -wal journal.
        with closing(sqlite3.connect(f"file:{SOURCE_DB.resolve().as_posix()}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(self.db_path)) as target:
                source.backup(target)
        self.store = review_ui.ReviewStore(self.db_path, root=ROOT)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.listening_id = conn.execute(
                "SELECT id FROM questions WHERE section_id LIKE '%listening' "
                "ORDER BY exam_number LIMIT 1"
            ).fetchone()[0]
            self.reading_id = conn.execute(
                "SELECT id FROM questions WHERE section_id LIKE '%reading' "
                "ORDER BY exam_number LIMIT 1"
            ).fetchone()[0]

    def _row(self, qid):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT stem,review_status,raw_question_text FROM questions WHERE id=?", (qid,)
            ).fetchone()

    def _choices(self, qid):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return [text for (text,) in conn.execute(
                "SELECT text FROM choices WHERE question_id=? ORDER BY number", (qid,)
            )]

    def _dialogue(self, qid):
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute("SELECT dialogue_text FROM transcripts WHERE question_id=?", (qid,)).fetchone()
            return row[0] if row else None

    def _answer(self, qid):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute("SELECT choice_number FROM answers WHERE question_id=?", (qid,)).fetchone()[0]

    def _history(self, qid):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT status,scope,evidence FROM review_records "
                "WHERE subject_type='question' AND subject_id=? ORDER BY id", (qid,)
            ).fetchall()

    def _payload(self, qid, *, status="verified", version=None, note="Compared with source PDF"):
        if version is None:
            version = self.store.get_question(qid)["version"]
        return {
            "version": version,
            "status": status,
            "stem": self._row(qid)[0],
            "choices": self._choices(qid),
            "transcript_text": self._dialogue(qid),
            "note": note,
        }

    def test_list_questions_and_detail_cover_existing_pilot(self):
        listing = self.store.list_questions()
        self.assertIsInstance(listing, dict)
        self.assertEqual(len(listing["items"]), 70)
        self.assertEqual(listing["counts"]["total"], 70)
        self.assertEqual(sum(listing["counts"][status] for status in
                             ("verified", "needs_manual_review", "rejected")), 70)
        ids = [self.store.get_question(qid) for qid in (self.listening_id, self.reading_id)]
        self.assertTrue(all(isinstance(item, dict) for item in ids))
        self.assertTrue(all(isinstance(item["version"], int) for item in ids))
        self.assertIn(self._answer(self.listening_id), (1, 2, 3, 4))

    def test_verified_pending_rejected_history_is_append_only(self):
        qid = self.listening_id
        original_answer = self._answer(qid)
        original_history = self._history(qid)
        for expected_status, note in (
            ("verified", "Matches page 3"),
            ("needs_manual_review", "Rechecking an image"),
            ("rejected", "Transcript has a confirmed typo"),
        ):
            result = self.store.save_review(qid, self._payload(qid, status=expected_status, note=note))
            self.assertIsInstance(result, dict)
            self.assertEqual(self._row(qid)[1], expected_status)
            self.assertEqual(self._answer(qid), original_answer)
        history = self._history(qid)
        self.assertEqual(len(history), len(original_history) + 3)
        self.assertEqual([row[0] for row in history[-3:]],
                         ["verified", "needs_manual_review", "rejected"])
        self.assertEqual(history[:len(original_history)], original_history)
        # A fresh connection/Store must see durable changes and the same log.
        reopened = review_ui.ReviewStore(self.db_path, root=ROOT)
        self.assertEqual(reopened.get_question(qid)["version"], self.store.get_question(qid)["version"])
        self.assertEqual(self._row(qid)[1], "rejected")

    def test_edits_persist_across_reopen_without_mutating_source_answers(self):
        qid = self.listening_id
        old_answer = self._answer(qid)
        old_choices = self._choices(qid)
        old_dialogue = self._dialogue(qid)
        payload = self._payload(qid)
        payload["stem"] = "Manually corrected stem"
        payload["choices"] = [f"Reviewed choice {i}" for i in range(1, 5)]
        payload["transcript_text"] = "여자: 검토한 대본입니다."
        self.store.save_review(qid, payload)
        reopened = review_ui.ReviewStore(self.db_path, root=ROOT)
        self.assertEqual(self._row(qid)[0], "Manually corrected stem")
        self.assertEqual(self._choices(qid), payload["choices"])
        self.assertEqual(self._dialogue(qid), payload["transcript_text"])
        self.assertEqual(self._answer(qid), old_answer)
        self.assertNotEqual(self._choices(qid), old_choices)
        self.assertNotEqual(self._dialogue(qid), old_dialogue)
        self.assertIsInstance(reopened.get_question(qid), dict)

    def test_optimistic_conflict_preserves_first_write_and_history(self):
        qid = self.listening_id
        stale = self._payload(qid)
        accepted = dict(stale, stem="First reviewer saved this", note="First review")
        self.store.save_review(qid, accepted)
        snapshot = (self._row(qid), self._choices(qid), self._dialogue(qid), self._history(qid))
        stale["stem"] = "Stale client must not overwrite"
        with self.assertRaises(review_ui.Conflict):
            self.store.save_review(qid, stale)
        self.assertEqual((self._row(qid), self._choices(qid), self._dialogue(qid), self._history(qid)),
                         snapshot)

    def test_rejected_requires_explanation_and_valid_status(self):
        qid = self.listening_id
        original = (self._row(qid), self._history(qid))
        for invalid in (dict(self._payload(qid, status="rejected"), note=""),
                        self._payload(qid, status="approved"),
                        self._payload(qid, status="VERIFIED")):
            with self.subTest(payload=invalid):
                with self.assertRaises(review_ui.ReviewError):
                    self.store.save_review(qid, invalid)
        self.assertEqual((self._row(qid), self._history(qid)), original)

    def test_invalid_choice_and_input_fail_without_partial_write(self):
        qid = self.listening_id
        baseline = (self._row(qid), self._choices(qid), self._answer(qid), self._history(qid))
        valid = self._payload(qid)
        invalid_payloads = (
            dict(valid, choices=["a", "b", "c"]),
            dict(valid, choices=["a", "b", "c", "d", "e"]),
            dict(valid, choices=["a", "b", 3, "d"]),
            dict(valid, stem=None),
            dict(valid, version="0"),
        )
        for bad in invalid_payloads:
            with self.subTest(payload=bad):
                with self.assertRaises(review_ui.ReviewError):
                    self.store.save_review(qid, bad)
                self.assertEqual((self._row(qid), self._choices(qid), self._answer(qid),
                                  self._history(qid)), baseline)

    def test_answer_is_immutable_even_when_submitted_as_extra_field(self):
        qid = self.listening_id
        current = self._answer(qid)
        original_history = self._history(qid)
        payload = self._payload(qid)
        payload["answer_choice"] = current % 4 + 1
        with self.assertRaises(review_ui.ReviewError):
            self.store.save_review(qid, payload)
        self.assertEqual(self._answer(qid), current)
        self.assertEqual(self._history(qid), original_history)

    def test_missing_ids_and_traversal_are_rejected(self):
        for malicious in ("missing-question", "../../etc/passwd", "..\\..\\Windows\\win.ini",
                          "%2e%2e%2fprivate", "", "035-I-L-001/../../secret"):
            with self.subTest(qid=malicious):
                with self.assertRaises((review_ui.ReviewError, review_ui.NotFound)):
                    self.store.get_question(malicious)
                with self.assertRaises((review_ui.ReviewError, review_ui.NotFound)):
                    self.store.media_path(malicious, "paper")
        for kind in ("../pilot-copy.sqlite", "/etc/passwd", "../../private", "image", "", "PAPER"):
            with self.subTest(kind=kind):
                with self.assertRaises(review_ui.ReviewError):
                    self.store.media_path(self.listening_id, kind)

    def test_media_is_source_scoped_and_images_are_binary(self):
        qid = self.listening_id
        for kind in ("paper", "answer", "transcript", "audio"):
            media = self.store.media_path(qid, kind)
            self.assertIsInstance(media, Path)
            self.assertTrue(media.is_file(), (kind, media))
            self.assertTrue(media.resolve().is_relative_to(ROOT.resolve()), (kind, media))
        with closing(sqlite3.connect(self.db_path)) as conn:
            image_qid = conn.execute("SELECT question_id FROM question_images LIMIT 1").fetchone()[0]
        image, mime = self.store.get_image(image_qid, 0)
        self.assertIsInstance(image, bytes)
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(mime, "image/png")
        with self.assertRaises((review_ui.ReviewError, review_ui.NotFound)):
            self.store.get_image(image_qid, 999)
        with self.assertRaises((review_ui.ReviewError, review_ui.NotFound)):
            self.store.get_image(image_qid, -1)
        with self.assertRaises(review_ui.NotFound):
            self.store.media_path(self.reading_id, "transcript")

    def test_poisoned_source_path_cannot_escape_35th_corpus(self):
        """Test actual DB-controlled path resolution, not only URL parameters."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            source_id = conn.execute(
                "SELECT source_file_id FROM questions WHERE id=?", (self.listening_id,)
            ).fetchone()[0]
            original = conn.execute(
                "SELECT relative_path FROM source_files WHERE id=?", (source_id,)
            ).fetchone()[0]
            for malicious in ("../../../outside.pdf", "topik-past-papers/52nd/52nd-TOPIK-I-Paper.pdf",
                              "C:/Windows/win.ini", "..\\..\\outside.pdf"):
                with self.subTest(malicious=malicious):
                    conn.execute("UPDATE source_files SET relative_path=? WHERE id=?", (malicious, source_id))
                    conn.commit()
                    with self.assertRaises(review_ui.ReviewError):
                        self.store.media_path(self.listening_id, "paper")
            conn.execute("UPDATE source_files SET relative_path=? WHERE id=?", (original, source_id))
            conn.commit()
        self.assertTrue(self.store.media_path(self.listening_id, "paper").is_file())


class TestReviewHTTP(unittest.TestCase):
    """Bind a disposable HTTP server only to loopback with a disposable DB."""

    @classmethod
    def setUpClass(cls):
        if not SOURCE_DB.is_file():
            raise unittest.SkipTest("Ignored 35th TOPIK I pilot SQLite is not installed")

    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="topik-review-http-test-")
        self.addCleanup(self.sandbox.cleanup)
        self.db_path = Path(self.sandbox.name) / "http-copy.sqlite"
        with closing(sqlite3.connect(f"file:{SOURCE_DB.resolve().as_posix()}?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(self.db_path)) as destination:
                original.backup(destination)
        self.store = review_ui.ReviewStore(self.db_path, root=ROOT)
        self.server = review_ui.ThreadingHTTPServer(("127.0.0.1", 0), review_ui.make_handler(self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # Shutdown before deleting the temp DB, including when an assertion fails.
        self.addCleanup(self._stop_server)
        self.host, self.port = self.server.server_address
        self.origin = f"http://127.0.0.1:{self.port}"

    def _stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive(), "HTTP review test server did not stop")

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def _get_json(self, path):
        code, headers, raw = self._request("GET", path)
        self.assertEqual(code, 200, raw)
        return json.loads(raw), headers

    def _post(self, qid, payload, *, origin=True, token=True, host=None, content_type="application/json"):
        headers = {"Content-Type": content_type}
        if origin:
            headers["Origin"] = self.origin if origin is True else origin
        if token:
            headers["X-Review-Token"] = self.token if token is True else token
        if host is not None:
            headers["Host"] = host
        return self._request("POST", f"/api/questions/{qid}/review",
                             json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers)

    def _payload(self):
        listing, _ = self._get_json("/api/questions")
        self.token = listing["csrf_token"]
        self.qid = listing["items"][0]["id"]
        detail, _ = self._get_json(f"/api/questions/{self.qid}")
        return {
            "version": detail["version"],
            "status": "verified",
            "stem": detail["stem"],
            "choices": [choice["text"] for choice in detail["choices"]],
            "transcript_text": detail["transcript"]["text"] if detail["transcript"] else None,
            "note": "Verified on private temporary database",
        }

    def _history_count(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM review_records WHERE subject_type='question'").fetchone()[0]

    def test_loopback_only_get_and_reviewer_token(self):
        self.assertEqual(self.host, "127.0.0.1")
        listing, headers = self._get_json("/api/questions")
        self.assertEqual(len(listing["items"]), 70)
        self.assertGreaterEqual(len(listing["csrf_token"]), 32)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        code, _, _ = self._request("GET", "/api/questions", headers={"Host": "evil.example"})
        self.assertEqual(code, 403)

    def test_post_requires_matching_host_origin_and_csrf_token(self):
        payload = self._payload()
        before = self._history_count()
        for variation in (
            {"origin": False},
            {"origin": "https://attacker.example"},
            {"origin": "null"},
            {"token": False},
            {"token": "wrong-token"},
            {"host": "attacker.example"},
        ):
            with self.subTest(variation=variation):
                status, _, raw = self._post(self.qid, payload, **variation)
                self.assertEqual(status, 403, raw)
                self.assertEqual(self._history_count(), before)
        code, _, raw = self._post(self.qid, payload)
        self.assertEqual(code, 200, raw)
        saved = json.loads(raw)
        self.assertEqual(saved["review_status"], "verified")
        self.assertEqual(self._history_count(), before + 1)

    def test_http_conflict_validation_and_transaction_integrity(self):
        payload = self._payload()
        code, _, response = self._post(self.qid, payload)
        self.assertEqual(code, 200, response)
        before = self._history_count()
        code, _, response = self._post(self.qid, payload)
        self.assertEqual(code, 409, response)
        self.assertEqual(self._history_count(), before)
        invalid = dict(payload, version=1, choices=["a", "b", "c"])
        code, _, response = self._post(self.qid, invalid)
        self.assertEqual(code, 400, response)
        code, _, response = self._post(self.qid, payload, content_type="text/plain")
        self.assertEqual(code, 415, response)
        self.assertEqual(self._history_count(), before)

    def test_http_media_ranges_and_traversal(self):
        self._payload()
        code, headers, body = self._request(
            "GET", f"/media/{self.qid}/paper", headers={"Range": "bytes=0-7"}
        )
        self.assertEqual(code, 206)
        self.assertEqual(body[:5], b"%PDF-")
        self.assertEqual(len(body), 8)
        self.assertTrue(headers["Content-Range"].startswith("bytes 0-7/"))
        code, _, body = self._request(
            "GET", f"/media/{self.qid}/paper", headers={"Range": "bytes=9999999999-"}
        )
        self.assertEqual(code, 416, body)
        for path in (f"/media/{self.qid}/../answer", "/media/%2e%2e%2fsecret/paper",
                     f"/media/{self.qid}/../../db/schema.sql"):
            with self.subTest(path=path):
                code, _, _ = self._request("GET", path)
                self.assertEqual(code, 404)


class TestReviewStartup(unittest.TestCase):
    def test_default_port_conflict_falls_back_to_local_os_selected_port(self):
        server = MagicMock()
        server.server_port = 54321
        server.__enter__.return_value = server
        server.serve_forever.side_effect = KeyboardInterrupt
        stdout, stderr = io.StringIO(), io.StringIO()
        handler = object()
        with patch.object(sys, "argv", ["review_ui.py"]), \
             patch.object(review_ui, "ReviewStore", return_value=object()), \
             patch.object(review_ui, "make_handler", return_value=handler), \
             patch.object(review_ui, "ThreadingHTTPServer",
                          side_effect=[OSError(errno.EACCES, "port unavailable"), server]) as bind, \
             redirect_stdout(stdout), redirect_stderr(stderr):
            review_ui.main()
        self.assertEqual(bind.call_args_list, [
            call(("127.0.0.1", 8765), handler), call(("127.0.0.1", 0), handler)
        ])
        self.assertIn("http://127.0.0.1:54321/", stdout.getvalue())
        self.assertIn("unavailable", stderr.getvalue())

    def test_explicit_port_conflict_does_not_silently_change_address(self):
        with patch.object(sys, "argv", ["review_ui.py", "--port", "8766"]), \
             patch.object(review_ui, "ReviewStore", return_value=object()), \
             patch.object(review_ui, "make_handler", return_value=object()), \
             patch.object(review_ui, "ThreadingHTTPServer",
                          side_effect=OSError(errno.EADDRINUSE, "port occupied")) as bind:
            with self.assertRaisesRegex(SystemExit, "Try --port 0"):
                review_ui.main()
        self.assertEqual(bind.call_count, 1)


if __name__ == "__main__":
    unittest.main()
