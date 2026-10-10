"""Human audio approval evidence, audited against an isolated 35th TOPIK copy.

The archived 035-I-B SQLite file is opened only with mode=ro/query_only, then
backed up to a temporary database. The registered MP3 is only ever read. HTTP
requests use a loopback server and all writes target the disposable copy.
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
FROZEN = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
PAIRS = ((25, 26), (27, 28), (29, 30))
NOTE = "원본 MP3 전체 문맥을 직접 듣고 시작과 끝, 대본 및 두 문항을 비교했습니다."


def question(number: int) -> str:
    return f"035-I-L-{number:03d}"


def evidence(**overrides):
    result = {
        "listened_to_source": True,
        "checked_start": True,
        "checked_end": True,
        "checked_transcript": True,
        "other_question_confirmed": True,
        "note": NOTE,
    }
    result.update(overrides)
    return result


class HumanAudioEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FROZEN.is_file():
            raise unittest.SkipTest("Frozen 35th corpus SQLite is unavailable")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="topik-audio-evidence-")
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "review.sqlite"
        with closing(sqlite3.connect(FROZEN.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            source.execute("PRAGMA query_only=ON")
            self.assertEqual(source.execute("PRAGMA query_only").fetchone()[0], 1)
            with closing(sqlite3.connect(self.db_path)) as target:
                source.backup(target)

        self.store = review_ui.ReviewStore(self.db_path, root=ROOT)
        self.server = review_ui.ThreadingHTTPServer(
            ("127.0.0.1", 0), review_ui.make_handler(self.store)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        status, listing = self.request("GET", "/api/questions")
        self.assertEqual(status, 200, listing)
        self.token = listing["csrf_token"]

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive())

    def request(self, method, endpoint, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {}
        if method == "POST":
            headers = {"Content-Type": "application/json",
                       "Origin": f"http://127.0.0.1:{self.server.server_port}",
                       "X-Review-Token": self.token}
        try:
            body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            conn.request(method, endpoint, headers=headers, body=body)
            result = conn.getresponse()
            body = result.read()
            return result.status, json.loads(body)
        finally:
            conn.close()

    def post(self, number, payload, *, exam_id=None):
        suffix = f"?exam_id={exam_id}" if exam_id else ""
        return self.request("POST", f"/api/questions/{question(number)}/audio-segment{suffix}",
                            payload)

    def segments(self, *numbers):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return {n: dict(conn.execute("SELECT * FROM audio_segments WHERE question_id=?",
                                         (question(n),)).fetchone()) for n in numbers}

    def history(self, number):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(
                "SELECT * FROM review_records WHERE subject_id=? ORDER BY id", (question(number),)
            )]

    def snapshot(self, numbers):
        """Capture all user text and approval rows, audit records, and media hashes."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            result = {}
            for n in numbers:
                qid = question(n)
                result[n] = {
                    table: conn.execute(f"SELECT * FROM {table} WHERE {column}=?",
                                        (qid,)).fetchall()
                    for table, column in (
                        ("questions", "id"), ("choices", "question_id"),
                        ("answers", "question_id"), ("transcripts", "question_id"),
                        ("audio_segments", "question_id"), ("review_records", "subject_id"),
                    )
                }
            result["source_files"] = conn.execute("SELECT * FROM source_files ORDER BY id").fetchall()
            result["audio_assets"] = conn.execute("SELECT * FROM audio_assets ORDER BY id").fetchall()
            result["total_history"] = conn.execute("SELECT count(*) FROM review_records").fetchone()[0]
            return result

    def current_payload(self, number, *, status="verified", include_evidence=True):
        segment = self.segments(number)[number]
        payload = {"start_ms": segment["start_ms"], "end_ms": segment["end_ms"],
                   "status": status, "version": segment["version"]}
        if include_evidence:
            payload["human_evidence"] = evidence()
        return payload

    def test_verified_requires_explicit_complete_human_evidence_http_400(self):
        for key in ("listened_to_source", "checked_start", "checked_end",
                    "checked_transcript", "other_question_confirmed"):
            for value in (False, None, 1, "true"):
                with self.subTest(key=key, invalid=value):
                    payload = self.current_payload(25)
                    payload["human_evidence"][key] = value
                    before = self.snapshot((25, 26))
                    code, response = self.post(25, payload)
                    self.assertEqual(code, 400, response)
                    self.assertEqual(self.snapshot((25, 26)), before)

        invalid_evidence = (
            None, {}, [], "heard it", evidence(note=""),
            evidence(note="too short"), evidence(note=" " * 30),
            evidence(note="x" * 2001), evidence(note=123),
            {key: value for key, value in evidence().items() if key != "checked_end"},
            dict(evidence(), autoplay_verified=True),
        )
        for item in invalid_evidence:
            with self.subTest(evidence=item):
                payload = self.current_payload(25)
                payload["human_evidence"] = item
                before = self.snapshot((25, 26))
                code, response = self.post(25, payload)
                self.assertEqual(code, 400, response)
                self.assertEqual(self.snapshot((25, 26)), before)

        before = self.snapshot((25, 26))
        code, response = self.post(25, self.current_payload(25, include_evidence=False))
        self.assertEqual(code, 400, response)
        self.assertEqual(self.snapshot((25, 26)), before)

    def test_candidate_requires_no_evidence_and_does_not_approve_question(self):
        before = self.snapshot((25, 26))
        previous_versions = {n: self.segments(n)[n]["version"] for n in (25, 26)}
        payload = self.current_payload(25, status="candidate", include_evidence=False)
        payload["start_ms"] += 1000
        payload["end_ms"] -= 1000
        code, response = self.post(25, payload)
        self.assertEqual(code, 200, response)
        after = self.snapshot((25, 26))
        for n in (25, 26):
            item = self.segments(n)[n]
            self.assertEqual((item["start_ms"], item["end_ms"], item["status"]),
                             (payload["start_ms"], payload["end_ms"], "candidate"))
            self.assertEqual(item["version"], previous_versions[n] + 1)
            self.assertEqual(after[n]["questions"], before[n]["questions"])
            self.assertEqual(after[n]["transcripts"], before[n]["transcripts"])
            self.assertEqual(after[n]["choices"], before[n]["choices"])
            self.assertEqual(len(after[n]["review_records"]),
                             len(before[n]["review_records"]) + 1)
            self.assertNotIn("human_evidence", json.loads(self.history(n)[-1]["evidence"]))
        self.assertEqual(after["source_files"], before["source_files"])
        self.assertEqual(after["audio_assets"], before["audio_assets"])

        # The UI or a background playback event cannot assert human verification
        # by sending evidence on a candidate save.
        bad_candidate = self.current_payload(26, status="candidate")
        code, response = self.post(26, bad_candidate)
        self.assertEqual(code, 400, response)
        self.assertEqual(self.snapshot((25, 26)), after)

    def test_all_shared_pairs_verified_on_exact_candidate_audit_both_members(self):
        for first, second in PAIRS:
            with self.subTest(pair=(first, second)):
                before = self.snapshot((first, second))
                old_a, old_b = self.segments(first, second).values()
                self.assertEqual((old_a["start_ms"], old_a["end_ms"], old_a["version"]),
                                 (old_b["start_ms"], old_b["end_ms"], old_b["version"]))
                payload = self.current_payload(second)
                code, response = self.post(second, payload)
                self.assertEqual(code, 200, response)
                self.assertEqual(response["audio_segment"]["status"], "verified")
                after = self.snapshot((first, second))
                saved = self.segments(first, second)
                for n in (first, second):
                    self.assertEqual(saved[n]["status"], "verified")
                    self.assertEqual(saved[n]["version"], old_a["version"] + 1)
                    self.assertEqual(saved[n]["source_sha256"], old_a["source_sha256"])
                    self.assertEqual(saved[n]["clip_relative_path"], None)
                    self.assertEqual(after[n]["questions"], before[n]["questions"])
                    self.assertEqual(after[n]["choices"], before[n]["choices"])
                    self.assertEqual(after[n]["transcripts"], before[n]["transcripts"])
                    self.assertEqual(after[n]["answers"], before[n]["answers"])
                    old_reviews = [r for r in before[n]["review_records"] if r[1] == "question"]
                    new_reviews = [r for r in after[n]["review_records"] if r[1] == "question"]
                    self.assertEqual(old_reviews, new_reviews,
                                     "Audio verification must not approve questions/transcripts")
                    self.assertEqual(len(after[n]["review_records"]),
                                     len(before[n]["review_records"]) + 1)
                    record = self.history(n)[-1]
                    self.assertEqual((record["subject_type"], record["status"], record["scope"]),
                                     ("audio_segment", "verified", "manual_audio_boundary_35"))
                    audit = json.loads(record["evidence"])
                    self.assertEqual(audit["human_evidence"], payload["human_evidence"])
                    self.assertEqual(audit["human_evidence"]["note"], NOTE)
                    self.assertEqual(audit["source_sha256"], old_a["source_sha256"])
                    self.assertEqual(audit["shared_questions"], [first, second])
                    self.assertIs(audit["verified_by_human_declaration"], True)
                self.assertEqual(after["source_files"], before["source_files"])
                self.assertEqual(after["audio_assets"], before["audio_assets"])

                # Second submit retains a stale token even if all values match.
                verified_snapshot = self.snapshot((first, second))
                code, response = self.post(second, payload)
                self.assertEqual(code, 409, response)
                self.assertEqual(self.snapshot((first, second)), verified_snapshot)

    def test_pair_mismatched_versions_and_changed_bounds_reject_atomically(self):
        before = self.snapshot((27, 28))
        payload = self.current_payload(27)
        payload["start_ms"] += 1000
        code, response = self.post(27, payload)
        self.assertEqual(code, 409, f"Actual saved candidate must be verified: {response}")
        self.assertEqual(self.snapshot((27, 28)), before)

        # A concurrent edit to only the sibling's version simulates a stale
        # reader observing a pair whose two CAS versions no longer agree.
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE audio_segments SET version=version+1 WHERE question_id=?",
                         (question(28),))
            conn.commit()
        mismatched = self.snapshot((27, 28))
        code, response = self.post(27, self.current_payload(27))
        self.assertEqual(code, 409, response)
        self.assertEqual(self.snapshot((27, 28)), mismatched)

    def test_second_sibling_evidence_insert_failure_rolls_back_everything(self):
        before = self.snapshot((29, 30))
        payload = self.current_payload(29)
        actual_connect = self.store._connect
        reached = []

        class InjectFailure:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, params=()):
                if sql.startswith("INSERT INTO review_records") and len(params) > 2 and \
                        params[1] == question(30) and params[2] == "verified":
                    reached.append("after_first_sibling_written")
                    raise sqlite3.OperationalError("injected evidence persistence failure")
                return self.connection.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        def connect(writable=False):
            conn = actual_connect(writable=writable)
            return InjectFailure(conn) if writable else conn

        with patch.object(self.store, "_connect", side_effect=connect):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected evidence"):
                self.store.save_audio_segment(question(29), payload)
        self.assertEqual(reached, ["after_first_sibling_written"])
        self.assertEqual(self.snapshot((29, 30)), before)

    def test_36_audio_approval_is_blocked_without_mutating_35(self):
        before = self.snapshot((25, 26))
        payload = self.current_payload(25)
        # Register exam 36 in the disposable database so rejection is caused by
        # the explicit editing policy, not by an unregistered exam ID.
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("INSERT INTO exams(id,session,level,booklet) VALUES(?,?,?,?)",
                         ("036-I-B", 36, "I", "B"))
            conn.commit()
        with self.assertRaises(review_ui.ReviewError):
            self.store.for_exam("036-I-B").save_audio_segment("036-I-L-025", payload)
        status, response = self.request(
            "POST", "/api/questions/036-I-L-025/audio-segment?exam_id=036-I-B", payload)
        self.assertEqual(status, 400, response)
        self.assertEqual(self.snapshot((25, 26)), before)


if __name__ == "__main__":
    unittest.main()
