"""Audio-segment API regressions against disposable copies of the 35 I pilot.

The corpus database and original MP3 are opened for reading only. All review
requests, deliberate metadata corruption, and schema upgrades affect a SQLite
backup in TemporaryDirectory and a server bound to an ephemeral loopback port.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src import review_ui


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
SCHEMA = ROOT / "db" / "schema.sql"
SOURCE_AUDIO = ROOT / "topik-past-papers" / "35th" / "35-TOPIK-I-Listening-Audio-File.mp3"


class TestAudioSegmentsAPI(unittest.TestCase):
    """Test persistence, provenance and HTTP policy without touching live data."""

    @classmethod
    def setUpClass(cls):
        if not SOURCE_DB.is_file():
            raise unittest.SkipTest("Local ignored 35 I pilot database is unavailable")
        from src.audio_35 import probe_audio

        # Probe once, read-only, then save only the duration in each TEMP copy.
        cls.duration_ms = probe_audio()
        if cls.duration_ms <= 0:
            raise AssertionError("Cannot verify audio interval bounds without source duration")

    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="topik-audio-segment-test-")
        self.addCleanup(self.sandbox.cleanup)
        self.db_path = Path(self.sandbox.name) / "review-copy.sqlite"
        with closing(sqlite3.connect(SOURCE_DB.resolve().as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(self.db_path)) as destination:
                original.backup(destination)  # Consistent even when the original uses WAL.

        # Schema application is idempotent and affects the temporary copy ONLY;
        # existing deployed pilot files may predate the new audio_segments table.
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript(SCHEMA.read_text(encoding="utf-8"))
            columns = {row[1] for row in db.execute("PRAGMA table_info(audio_segments)")}
            self.assertTrue({"question_id", "start_ms", "end_ms", "status", "version"} <= columns)
            db.execute("DELETE FROM audio_segments")
            db.execute("UPDATE audio_assets SET duration_seconds=? "
                       "WHERE section_id='035-I-B-listening'", (self.duration_ms / 1000,))
            db.commit()

        self.store = review_ui.ReviewStore(self.db_path, root=ROOT)
        self.server = review_ui.ThreadingHTTPServer(("127.0.0.1", 0), review_ui.make_handler(self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        listing, headers = self._get_json("/api/questions")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.token = listing["csrf_token"]
        self.assertGreaterEqual(len(self.token), 32)

    def _stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive())

    def _request(self, method, path, *, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            response_headers = dict(response.getheaders())
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                data = raw
            return response.status, response_headers, data
        finally:
            connection.close()

    def _get_json(self, path):
        code, headers, data = self._request("GET", path)
        self.assertEqual(code, 200, data)
        self.assertIsInstance(data, dict)
        return data, headers

    def _detail(self, number):
        section = "L" if number <= 30 else "R"
        question_id = f"035-I-{section}-{number:03d}"
        detail, _ = self._get_json(f"/api/questions/{question_id}")
        self.assertEqual(detail["id"], question_id)
        self.assertIn("audio_segment", detail)
        return detail

    def _post(self, number, payload, *, endpoint="audio-segment", origin=None,
              token=None, host=None, content_type="application/json"):
        section = "L" if number <= 30 else "R"
        qid = f"035-I-{section}-{number:03d}"
        headers = {"Content-Type": content_type,
                   "Origin": self.origin if origin is None else origin,
                   "X-Review-Token": self.token if token is None else token}
        if host is not None:
            headers["Host"] = host
        return self._request("POST", f"/api/questions/{qid}/{endpoint}",
                             payload=payload, headers=headers)

    def _payload(self, number=1, *, start=10000, end=13000, status="candidate"):
        segment = self._detail(number)["audio_segment"]
        return {"version": segment["version"] if segment else 0,
                "start_ms": start, "end_ms": end, "status": status}

    def _rows(self, *numbers):
        with closing(sqlite3.connect(self.db_path)) as db:
            return {number: db.execute(
                "SELECT start_ms,end_ms,status,version FROM audio_segments WHERE question_id=?",
                (f"035-I-L-{number:03d}",),
            ).fetchone() for number in numbers}

    def _unchanged_content(self, number=1):
        with closing(sqlite3.connect(self.db_path)) as db:
            qid = f"035-I-L-{number:03d}"
            question = db.execute("SELECT stem,review_status,points FROM questions WHERE id=?", (qid,)).fetchone()
            answer = db.execute("SELECT choice_number,source_file_id FROM answers WHERE question_id=?", (qid,)).fetchone()
            transcript = db.execute("SELECT dialogue_text,review_status FROM transcripts WHERE question_id=?", (qid,)).fetchone()
            return question, answer, transcript

    def test_initial_detail_and_reading_have_no_approved_clip(self):
        listening = self._detail(1)
        reading = self._detail(31)
        self.assertIsNone(listening["audio_segment"])
        self.assertIsNone(reading["audio_segment"])
        self.assertEqual(self._rows(1)[1], None)
        code, _, _ = self._post(31, {"version": 0, "start_ms": 1000,
                                    "end_ms": 2000, "status": "candidate"})
        self.assertIn(code, (400, 404))
        self.assertIsNone(self._detail(31)["audio_segment"])

    def test_candidate_round_trip_never_approves_question_or_exports_clip(self):
        original = self._unchanged_content()
        code, _, data = self._post(1, self._payload())
        self.assertEqual(code, 200, data)
        detail = self._detail(1)
        segment = detail["audio_segment"]
        self.assertEqual((segment["start_ms"], segment["end_ms"], segment["status"]),
                         (10000, 13000, "candidate"))
        self.assertIsInstance(segment["version"], int)
        self.assertGreater(segment["version"], 0)
        self.assertGreater(segment["source_duration_ms"], 13000)
        self.assertIsNone(segment["clip_url"])
        self.assertEqual(self._unchanged_content(), original)
        self.assertEqual(self._rows(1)[1][:3], (10000, 13000, "candidate"))
        code, _, _ = self._post(1, {}, endpoint="export-clip")
        self.assertIn(code, (400, 409))
        self.assertIsNone(self._detail(1)["audio_segment"]["clip_url"])

    def test_strict_time_type_order_range_and_payload_validation(self):
        code, _, data = self._post(1, self._payload())
        self.assertEqual(code, 200, data)
        current = self._detail(1)["audio_segment"]
        snapshot = self._rows(1)[1]
        baseline = self._unchanged_content()
        valid = self._payload()
        bad = [
            dict(valid, start_ms=-1), dict(valid, end_ms=0),
            dict(valid, start_ms=15000, end_ms=15000),
            dict(valid, start_ms=16000, end_ms=15000),
            dict(valid, start_ms=True), dict(valid, end_ms=False),
            dict(valid, start_ms=1000.5), dict(valid, end_ms="13000"),
            dict(valid, status="approved"), dict(valid, status=None),
            dict(valid, end_ms=current["source_duration_ms"] + 1),
            dict(valid, unexpected="cannot modify other columns"),
            {key: val for key, val in valid.items() if key != "version"},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                code, _, data = self._post(1, payload)
                self.assertEqual(code, 400, data)
                self.assertEqual(self._rows(1)[1], snapshot)
        self.assertEqual(self._unchanged_content(), baseline)

    def test_stale_version_cannot_overwrite_saved_candidate(self):
        stale = self._payload()
        code, _, data = self._post(1, stale)
        self.assertEqual(code, 200, data)
        snapshot = self._rows(1)[1]
        stale.update(start_ms=20000, end_ms=24000)
        code, _, _ = self._post(1, stale)
        self.assertEqual(code, 409)
        self.assertEqual(self._rows(1)[1], snapshot)

    def test_all_three_shared_dialogue_pairs_mirror_atomically(self):
        for left, right in ((25, 26), (27, 28), (29, 30)):
            with self.subTest(pair=(left, right)):
                original = (self._unchanged_content(left), self._unchanged_content(right))
                candidate = self._payload(left, start=20000, end=27000)
                code, _, data = self._post(left, candidate)
                self.assertEqual(code, 200, data)
                a = self._detail(left)["audio_segment"]
                b = self._detail(right)["audio_segment"]
                for field in ("start_ms", "end_ms", "status", "source_duration_ms"):
                    self.assertEqual(a[field], b[field], field)
                self.assertEqual((a["start_ms"], a["end_ms"], a["status"]),
                                 (20000, 27000, "candidate"))
                self.assertGreater(b["version"], 0)
                # Reviewing from either member must update both, and invalidate
                # the version held by the first tab.
                verified = {"version": b["version"], "start_ms": 21000,
                            "end_ms": 27500, "status": "verified"}
                code, _, data = self._post(right, verified)
                self.assertEqual(code, 200, data)
                self.assertEqual(self._rows(left, right)[left][:3], (21000, 27500, "verified"))
                self.assertEqual(self._rows(left, right)[right][:3], (21000, 27500, "verified"))
                code, _, _ = self._post(left, candidate)
                self.assertEqual(code, 409)
                self.assertEqual((self._unchanged_content(left), self._unchanged_content(right)), original)

    def test_host_origin_token_and_content_type_requirements(self):
        payload = self._payload()
        unauthorized = (
            {"origin": "https://attacker.example"},
            {"origin": "null"},
            {"token": "invalid-token"},
            {"token": ""},
            {"host": "attacker.example"},
        )
        for variation in unauthorized:
            with self.subTest(variation=variation):
                code, _, _ = self._post(1, payload, **variation)
                self.assertEqual(code, 403)
                self.assertIsNone(self._rows(1)[1])
        code, _, _ = self._post(1, payload, content_type="text/plain")
        self.assertEqual(code, 415)
        self.assertIsNone(self._rows(1)[1])

    def test_source_sha_mismatch_blocks_verified_clip_export(self):
        code, _, data = self._post(1, self._payload())
        self.assertEqual(code, 200, data)
        candidate = self._detail(1)["audio_segment"]
        code, _, data = self._post(1, {"version": candidate["version"],
                                      "start_ms": 10000, "end_ms": 13000,
                                      "status": "verified"})
        self.assertEqual(code, 200, data)
        with closing(sqlite3.connect(self.db_path)) as db:
            source = db.execute(
                "SELECT source_file_id FROM audio_assets WHERE section_id='035-I-B-listening'"
            ).fetchone()[0]
            db.execute("UPDATE source_files SET sha256=? WHERE id=?", ("0" * 64, source))
            db.commit()  # Corrupt metadata in the disposable copy, never the MP3.
        code, _, _ = self._post(1, {}, endpoint="export-clip")
        self.assertNotEqual(code, 200, "Export must verify audio bytes against recorded source SHA")
        self.assertIsNone(self._detail(1)["audio_segment"]["clip_url"])

    def test_verified_export_uses_temporary_folder_and_shares_one_clip(self):
        """Mock the encoder so this never writes an MP3 into the real corpus."""
        candidate = self._payload(25, start=20000, end=27000)
        code, _, data = self._post(25, candidate)
        self.assertEqual(code, 200, data)
        segment = self._detail(26)["audio_segment"]
        code, _, data = self._post(26, {"version": segment["version"],
                                       "start_ms": 20000, "end_ms": 27000,
                                       "status": "verified"})
        self.assertEqual(code, 200, data)
        self.assertIsNone(self._detail(25)["audio_segment"]["clip_url"])

        fake_mp3 = b"ID3" + b"\x00" * 2048

        def fake_encoder(source, destination, start_ms, end_ms):
            self.assertEqual(Path(source).resolve(), SOURCE_AUDIO.resolve())
            self.assertEqual((start_ms, end_ms), (20000, 27000))
            # The real audio_35.export_segment refuses to overwrite an
            # existing destination; mkstemp leaves a file and would fail.
            self.assertFalse(Path(destination).exists(),
                             "Encoder must receive a new, not-yet-created .mp3 path")
            Path(destination).write_bytes(fake_mp3)

        # review_ui normally exports under store.root; redirect that entire
        # destination to a disposable root. Only media_path is mocked to read
        # the unchanged actual source, and FFmpeg is never invoked.
        with patch.object(self.store, "root", Path(self.sandbox.name)), \
             patch.object(self.store, "media_path", return_value=SOURCE_AUDIO), \
             patch("src.audio_35.export_segment", side_effect=fake_encoder) as encoder:
            code, _, data = self._post(25, {}, endpoint="export-clip")
            self.assertEqual(code, 200, data)
            self.assertEqual(encoder.call_count, 1)
            left = self._detail(25)["audio_segment"]
            right = self._detail(26)["audio_segment"]
            self.assertEqual(left["clip_url"], "/media/035-I-L-025/clip")
            self.assertEqual(right["clip_url"], "/media/035-I-L-026/clip")
            code, headers, content = self._request("GET", left["clip_url"])
            self.assertEqual(code, 200)
            self.assertEqual(headers.get("Content-Type"), "audio/mpeg")
            self.assertEqual(content, fake_mp3)
            self.assertEqual(headers.get("Cache-Control"), "no-store")
            clip_root = Path(self.sandbox.name) / "topik-past-papers" / "derived" / "audio-clips"
            self.assertEqual(len(list(clip_root.glob("*.mp3"))), 1)

        with closing(sqlite3.connect(self.db_path)) as db:
            clips = db.execute("SELECT clip_relative_path,clip_sha256 FROM audio_segments "
                               "WHERE question_id IN ('035-I-L-025','035-I-L-026') "
                               "ORDER BY question_id").fetchall()
        self.assertEqual(clips[0], clips[1], "Shared dialogue must reference one identical export")


if __name__ == "__main__":
    unittest.main()
