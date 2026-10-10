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
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from src import review_ui
from src import ai_audit_35


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

    def test_fast_navigation_reads_skip_expensive_ai_audit_queries(self):
        with patch.object(self.store, "_ai_audit_summaries", side_effect=AssertionError("AI list lookup")), \
             patch.object(self.store, "_get_ai_audit_for_question", side_effect=AssertionError("AI detail lookup")):
            listing = self.store.list_questions_fast()
            detail = self.store.get_question(self.listening_id, fast=True)
        self.assertEqual(listing["counts"]["total"], 70)
        self.assertFalse(listing["ai_audit_available"])
        self.assertTrue(all("ai_audit" not in item for item in listing["items"]))
        self.assertEqual(detail["id"], self.listening_id)
        self.assertEqual(len(detail["choices"]), 4)
        self.assertNotIn("ai_audit", detail)

    def test_fast_navigation_bulk_manual_review_evidence_distinguishes_current_and_historical(self):
        qid_current, qid_superseded, qid_unproven = [
            item["id"] for item in self.store.list_questions_fast()["items"][:3]
        ]
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "DELETE FROM review_records WHERE subject_type='question' AND subject_id IN (?,?,?)",
                (qid_current, qid_superseded, qid_unproven),
            )
            conn.executemany(
                "UPDATE questions SET review_status='verified' WHERE id=?",
                [(qid_current,), (qid_superseded,), (qid_unproven,)],
            )
            ids = []
            for subject_id in (qid_current, qid_superseded):
                ids.append(conn.execute(
                    "INSERT INTO review_records(subject_type, subject_id, status, reviewer, scope, evidence, reviewed_at) "
                    "VALUES('question',?,'verified','local_reviewer','manual_question_review','{}',?)",
                    (subject_id, "2026-10-08T12:00:00+00:00"),
                ).lastrowid)
            conn.execute(
                "INSERT INTO review_records(subject_type, subject_id, status, reviewer, scope, evidence, reviewed_at) "
                "VALUES('question',?,'verified',NULL,'automatic_correction','{}',?)",
                (qid_superseded, "2026-10-08T12:30:00+00:00"),
            )
            conn.commit()

        original_connect = self.store._connect
        sql_calls = []

        class CountingConnection:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, params=()):
                sql_calls.append(sql)
                return self.db.execute(sql, params)

            def close(self):
                self.db.close()

        with patch.object(self.store, "_connect",
                          side_effect=lambda: CountingConnection(original_connect())):
            listing = self.store.list_questions_fast()
        self.assertEqual(len(sql_calls), 1, "The 70-question listing must use a bulk query")
        self.assertIn("ROW_NUMBER()", sql_calls[0])
        self.assertEqual(len(listing["items"]), 70)
        rows = {item["id"]: item for item in listing["items"]}
        self.assertEqual(rows[qid_current]["review_version"], 1)
        self.assertEqual(rows[qid_current]["status"], "verified")
        self.assertEqual(rows[qid_current]["last_human_review"]["id"], ids[0])
        self.assertEqual(rows[qid_current]["last_human_review"]["reviewed_at"],
                         "2026-10-08T12:00:00+00:00")
        self.assertTrue(rows[qid_current]["last_human_review"]["approved"])
        self.assertTrue(rows[qid_current]["last_human_review"]["is_current"])
        self.assertEqual(rows[qid_superseded]["review_version"], 2)
        self.assertEqual(rows[qid_superseded]["last_human_review"]["id"], ids[1])
        self.assertEqual(rows[qid_superseded]["last_human_review"]["status"], "verified")
        self.assertFalse(rows[qid_superseded]["last_human_review"]["approved"])
        self.assertFalse(rows[qid_superseded]["last_human_review"]["is_current"])
        self.assertEqual(rows[qid_unproven]["status"], "verified")
        self.assertIsNone(rows[qid_unproven]["last_human_review"])
        self.assertEqual(rows[qid_unproven]["review_version"], 0)

    def test_fast_navigation_query_works_with_postgres_style_dict_rows(self):
        # Exercise the shared SQL with an adapter returning psycopg-like
        # mappings. No live PostgreSQL service or data is modified.
        expected = self.store.list_questions_fast()

        class DictCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchall(self):
                return [dict(row) for row in self.cursor.fetchall()]

        class PostgresStyleRead:
            backend = "postgres"

            def __init__(self, db_path):
                self.db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
                self.db.row_factory = sqlite3.Row

            def execute(self, sql, params=()):
                return DictCursor(self.db.execute(sql, params))

            def close(self):
                self.db.close()

        with patch.object(self.store, "backend", "postgres"), \
             patch.object(self.store, "_connect",
                          side_effect=lambda: PostgresStyleRead(self.db_path)):
            listing = self.store.list_questions_fast()
        self.assertEqual(listing["database_backend"], "postgres")
        self.assertEqual(listing["items"], expected["items"])
        self.assertEqual(listing["counts"], expected["counts"])

    def test_manual_approval_evidence_agrees_across_fast_list_detail_and_f3_bundle(self):
        """Stored verified is not equivalent to current human approval."""
        qid = self.reading_id

        def surfaces():
            row = next(item for item in self.store.list_questions_fast()["items"]
                       if item["id"] == qid)
            detail = self.store.get_question(qid, fast=True)
            bundle = self.store.get_questions_bundle()["questions"][qid]
            for item in (detail, bundle):
                self.assertEqual(item["review_status"], row["status"])
                self.assertEqual(item["last_human_review"], row["last_human_review"])
                self.assertEqual(item["human_review_evidence"], row["human_review_evidence"])
                self.assertEqual(item["version"], row["review_version"])
            return row, detail, bundle

        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("DELETE FROM review_records WHERE subject_type='question' AND subject_id=?", (qid,))
            db.execute("UPDATE questions SET review_status='verified' WHERE id=?", (qid,))
            db.commit()
        row, _, _ = surfaces()
        self.assertIsNone(row["last_human_review"])
        self.assertEqual(row["human_review_evidence"]["reason"], "missing_manual_review")
        self.assertFalse(row["human_review_evidence"]["approved"])

        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                "VALUES('question',?,'verified',NULL,'manual_question_review','{}',?)",
                (qid, "2026-10-10T01:00:00+00:00"),
            )
            db.commit()
        row, _, _ = surfaces()
        self.assertEqual(row["human_review_evidence"]["reason"], "incomplete_manual_review")
        self.assertFalse(row["last_human_review"]["is_current"])

        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                "VALUES('question',?,'verified','local_reviewer','manual_question_review','{}',?)",
                (qid, "2026-10-10T02:00:00+00:00"),
            )
            db.commit()
        row, detail, bundle = surfaces()
        self.assertTrue(row["last_human_review"]["approved"])
        self.assertTrue(row["last_human_review"]["is_current"])
        self.assertEqual(row["human_review_evidence"]["approval_state"], "current_manual_approval")
        self.assertFalse(row["last_human_review"]["identity_verified"])
        self.assertFalse(row["human_review_evidence"]["identity_verified"])
        self.assertEqual(detail["history"][0]["scope"], "manual_question_review")
        self.assertEqual(bundle["history"][0]["id"], row["last_human_review"]["id"])

        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                "VALUES('question',?,'verified','automated_extractor','automatic_correction','{}',?)",
                (qid, "2026-10-10T03:00:00+00:00"),
            )
            db.commit()
        row, _, _ = surfaces()
        self.assertEqual(row["human_review_evidence"]["reason"], "superseded_manual_review")
        self.assertFalse(row["last_human_review"]["approved"])
        self.assertFalse(row["last_human_review"]["is_current"])

        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE questions SET review_status='needs_manual_review' WHERE id=?", (qid,))
            db.commit()
        row, _, _ = surfaces()
        self.assertEqual(row["human_review_evidence"]["approval_state"], "not_verified")
        self.assertFalse(row["human_review_evidence"]["approved"])

    def test_fast_ack_contains_bounded_current_history_without_a_followup_get(self):
        qid = self.reading_id
        before = self.store.get_question(qid, fast=True)
        payload = self._payload(qid, status="verified", note="Compared with the source PDF")
        with patch.object(self.store, "get_question", side_effect=AssertionError("redundant detail GET")):
            ack = self.store.save_review(qid, payload, fast_response=True)
        self.assertTrue(ack["saved"])
        self.assertEqual(ack["human_review_evidence"]["approval_state"], "current_manual_approval")
        self.assertEqual(ack["saved_review_event"], ack["history"][0])
        self.assertEqual(ack["saved_review_event"]["scope"], "manual_question_review")
        self.assertEqual(ack["saved_review_event"]["id"], ack["saved_review_id"])
        self.assertEqual(ack["saved_review_event"]["note"], "Compared with the source PDF")
        self.assertEqual(ack["history"], self.store.get_question(qid, fast=True)["history"])
        list_row = next(row for row in self.store.list_questions_fast()["items"] if row["id"] == qid)
        self.assertEqual(ack["last_human_review"], list_row["last_human_review"])
        self.assertEqual(ack["version"], before["version"] + 1)
        no_op = dict(payload, version=ack["version"], note="")
        again = self.store.save_review(qid, no_op, fast_response=True)
        self.assertFalse(again["saved"])
        self.assertIsNone(again["saved_review_event"])
        self.assertEqual(again["history"], ack["history"])
        self.assertEqual(again["last_human_review"], ack["last_human_review"])

    def test_older_manual_event_stays_visible_as_stale_beyond_history_limit(self):
        qid = self.reading_id
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("DELETE FROM review_records WHERE subject_type='question' AND subject_id=?", (qid,))
            db.execute("UPDATE questions SET review_status='verified' WHERE id=?", (qid,))
            original_manual = db.execute(
                "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                "VALUES('question',?,'verified','local_reviewer','manual_question_review','{}',?)",
                (qid, "2026-10-09T12:00:00+00:00"),
            ).lastrowid
            for i in range(35):
                db.execute(
                    "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                    "VALUES('question',?,'verified',NULL,'automatic_correction','{}',?)",
                    (qid, f"2026-10-10T12:{i:02d}:00+00:00"),
                )
            db.commit()
        list_row = next(row for row in self.store.list_questions_fast()["items"] if row["id"] == qid)
        detail = self.store.get_question(qid, fast=True)
        bundle = self.store.get_questions_bundle()["questions"][qid]
        payload = self._payload(qid, status="verified", note="")
        ack = self.store.save_review(qid, payload, fast_response=True)
        self.assertFalse(ack["saved"])
        for row in (list_row, detail, bundle, ack):
            self.assertEqual(row["last_human_review"]["id"], original_manual)
            self.assertFalse(row["last_human_review"]["approved"])
            self.assertEqual(row["human_review_evidence"]["reason"], "superseded_manual_review")
        for row in (detail, bundle, ack):
            self.assertEqual(len(row["history"]), 30)
            self.assertTrue(all(entry["scope"] != "manual_question_review" for entry in row["history"]))

    def test_fast_review_conflict_and_rollback_cannot_fabricate_human_approval(self):
        qid = self.reading_id
        before = self.store.get_question(qid, fast=True)
        proposed = self._payload(qid, status="verified", note="Checked registered PDF")
        stale = dict(proposed)
        saved = self.store.save_review(qid, proposed, fast_response=True)
        self.assertTrue(saved["last_human_review"]["approved"])
        snapshot = (self._row(qid), self._history(qid), self.store.get_question(qid, fast=True))
        with self.assertRaises(review_ui.Conflict):
            self.store.save_review(qid, stale, fast_response=True)
        self.assertEqual((self._row(qid), self._history(qid),
                          self.store.get_question(qid, fast=True)), snapshot)

        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("CREATE TRIGGER private_fail_manual_evidence BEFORE INSERT ON review_records "
                       "WHEN NEW.scope='manual_question_review' BEGIN "
                       "SELECT RAISE(ABORT,'private injected review-record failure'); END")
            db.commit()
        rejected = self._payload(qid, status="rejected", note="Source contradicted earlier approval")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.save_review(qid, rejected, fast_response=True)
        self.assertEqual((self._row(qid), self._history(qid),
                          self.store.get_question(qid, fast=True)), snapshot)
        self.assertEqual(self.store.get_question(qid, fast=True)["version"], before["version"] + 1)

    def test_manual_status_mismatch_and_rejected_status_remain_nonapproved(self):
        qid = self.reading_id
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("DELETE FROM review_records WHERE subject_type='question' AND subject_id=?", (qid,))
            db.execute("UPDATE questions SET review_status='verified' WHERE id=?", (qid,))
            db.execute(
                "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                "VALUES('question',?,'rejected','local_reviewer','manual_question_review','{}',?)",
                (qid, "2026-10-10T03:00:00+00:00"),
            )
            db.commit()
        for item in (
            next(item for item in self.store.list_questions_fast()["items"] if item["id"] == qid),
            self.store.get_question(qid, fast=True),
            self.store.get_questions_bundle()["questions"][qid],
        ):
            self.assertEqual(item["human_review_evidence"]["reason"], "manual_status_mismatch")
            self.assertFalse(item["last_human_review"]["is_current"])
            self.assertFalse(item["last_human_review"]["approved"])

        payload = self._payload(qid, status="rejected", note="Explicit rejection note")
        ack = self.store.save_review(qid, payload, fast_response=True)
        self.assertEqual(ack["last_human_review"]["status"], "rejected")
        self.assertTrue(ack["last_human_review"]["is_current"])
        self.assertFalse(ack["last_human_review"]["approved"])
        self.assertEqual(ack["human_review_evidence"]["approval_state"], "not_verified")
        self.assertEqual(ack["history"][0], ack["saved_review_event"])
        self.assertEqual(ack["history"][0]["note"], "Explicit rejection note")

    def test_human_evidence_does_not_leak_between_actual_35_and_36_exam_sources(self):
        """Private SQLite adds a 36th fixture linked to an immutable real 36th PDF."""
        pdf = ROOT / "topik-past-papers" / "36th" / "36th-TOPIK-I-Reading-Test-Paper.pdf"
        if not pdf.is_file():
            self.skipTest("Real 36th reading PDF not installed")
        qid_36 = "036-I-R-031"
        exam = "036-I-B"
        section = "036-I-B-reading"
        group = "036-I-R-31"
        logical = "topik-past-papers/36th/36th-TOPIK-I-Reading-Test-Paper.pdf"
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "INSERT INTO source_files(relative_path,kind,sha256,byte_size) VALUES(?,?,?,?)",
                (logical, "test_paper", _digest(pdf), pdf.stat().st_size),
            )
            source_id = db.execute(
                "SELECT id FROM source_files WHERE relative_path=?", (logical,)
            ).fetchone()[0]
            db.execute("INSERT INTO exams(id,session,level,booklet) VALUES(?,36,'I','B')", (exam,))
            db.execute(
                "INSERT INTO sections(id,exam_id,name,first_exam_number,last_exam_number) "
                "VALUES(?,?,'reading',31,70)", (section, exam),
            )
            db.execute(
                "INSERT INTO question_groups(id,section_id,first_exam_number,last_exam_number,instruction) "
                "VALUES(?,?,31,31,'36 reading source')", (group, section),
            )
            db.execute(
                "INSERT INTO questions(id,section_id,group_id,source_file_id,exam_number,"
                "answer_key_number,source_pdf_page,points,stem,raw_question_text,extraction_origin,review_status) "
                "VALUES(?,?,?,?,31,31,1,2,'36 test item','36 raw','source_pdf','verified')",
                (qid_36, section, group, source_id),
            )
            db.executemany(
                "INSERT INTO choices(question_id,number,text) VALUES(?,?,?)",
                [(qid_36, number, f"36 option {number}") for number in range(1, 5)],
            )
            db.execute(
                "INSERT INTO answers(question_id,choice_number,source_file_id,source_pdf_page,"
                "preview_and_pdf_agree) VALUES(?,1,?,1,1)", (qid_36, source_id),
            )
            db.commit()

        store_36 = self.store.for_exam(exam)
        default_before = self.store.get_question(self.reading_id, fast=True)
        for item in (
            store_36.list_questions_fast()["items"][0],
            store_36.get_question(qid_36, fast=True),
            store_36.get_questions_bundle()["questions"][qid_36],
        ):
            self.assertFalse(item["human_review_evidence"]["approved"])
            self.assertEqual(item["human_review_evidence"]["reason"], "missing_manual_review")
            self.assertIsNone(item["last_human_review"])

        payload = {
            "version": 0, "status": "verified", "stem": "36 test item",
            "choices": [f"36 option {number}" for number in range(1, 5)],
            "transcript_text": None, "note": "Reviewed the actual 36th reading PDF",
        }
        ack = store_36.save_review(qid_36, payload, fast_response=True)
        self.assertTrue(ack["last_human_review"]["approved"])
        self.assertEqual(ack["version"], 1)
        self.assertEqual(ack["human_review_evidence"]["reason"], "latest_manual_review")
        self.assertEqual(self.store.list_questions_fast()["counts"]["total"], 70)
        self.assertEqual(store_36.list_questions_fast()["counts"]["total"], 1)
        self.assertEqual(self.store.get_question(self.reading_id, fast=True)["version"],
                         default_before["version"])
        self.assertEqual(self.store.get_question(self.reading_id, fast=True)["last_human_review"],
                         default_before["last_human_review"])
        with self.assertRaises(review_ui.NotFound):
            self.store.get_question(qid_36, fast=True)

    def test_fast_review_commit_ack_preserves_version_and_source_validation(self):
        qid = self.listening_id
        before = self.store.get_question(qid, fast=True)
        history_before = self._history(qid)
        no_change = self._payload(qid, status=before["review_status"], note="")
        with patch.object(self.store, "media_path", side_effect=AssertionError("redundant connection")):
            ack = self.store.save_review(qid, no_change, fast_response=True)
        self.assertEqual(ack["version"], before["version"])
        self.assertEqual(ack["request_version"], before["version"])
        self.assertEqual(ack["saved_version"], before["version"])
        self.assertFalse(ack["saved"])
        self.assertIsNone(ack["saved_review_id"])
        self.assertIsNone(ack["saved_reviewed_at"])
        self.assertEqual(self._history(qid), history_before)

        approved = dict(no_change, status="verified", note="Original PDF checked")
        with patch.object(self.store, "get_question", side_effect=AssertionError("full reread")):
            ack = self.store.save_review(qid, approved, fast_response=True)
        self.assertEqual(ack["id"], qid)
        self.assertEqual(ack["review_status"], "verified")
        self.assertEqual(ack["version"], before["version"] + 1)
        self.assertEqual(ack["request_version"], before["version"])
        self.assertEqual(ack["saved_version"], before["version"] + 1)
        self.assertTrue(ack["saved"])
        self.assertIsInstance(ack["saved_review_id"], int)
        self.assertTrue(ack["saved_reviewed_at"])
        self.assertEqual(ack["last_human_review"]["id"], ack["saved_review_id"])
        self.assertEqual(ack["last_human_review"]["status"], "verified")
        self.assertEqual(ack["last_human_review"]["reviewed_at"], ack["saved_reviewed_at"])
        self.assertTrue(ack["last_human_review"]["is_current"])
        self.assertTrue(ack["last_human_review"]["approved"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            actual = conn.execute(
                "SELECT id, reviewed_at FROM review_records WHERE subject_type='question' "
                "AND subject_id=? AND scope='manual_question_review' ORDER BY id DESC LIMIT 1",
                (qid,),
            ).fetchone()
        self.assertEqual((ack["saved_review_id"], ack["saved_reviewed_at"]), actual)
        self.assertEqual(self._row(qid)[1], "verified")
        self.assertEqual(len(self._history(qid)), len(history_before) + 1)
        no_op_again = dict(approved, version=ack["version"], note="")
        after_noop = self.store.save_review(qid, no_op_again, fast_response=True)
        self.assertFalse(after_noop["saved"])
        self.assertEqual(after_noop["request_version"], ack["version"])
        self.assertEqual(after_noop["version"], ack["version"])
        self.assertEqual(after_noop["last_human_review"], ack["last_human_review"])
        self.assertIsNone(after_noop["saved_review_id"])
        with self.assertRaises(review_ui.Conflict):
            self.store.save_review(qid, approved, fast_response=True)

    def test_independent_comparison_uses_actual_audit_provenance_and_unknown_archive_time(self):
        # The complete 70-item fixtures use the production SQLite schema, and
        # live only in this test's private SQLite backup.
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
            question_ids = [row[0] for row in conn.execute(
                "SELECT id FROM questions ORDER BY exam_number"
            )]
            self.assertEqual(len(question_ids), 70)
            run_time = "2026-10-07T04:20:00+00:00"
            pass_time = "2026-10-07T04:21:00+00:00"
            result_time = "2026-10-07T04:25:00+00:00"
            snapshot_time = "2026-10-07T04:19:00+00:00"
            conn.execute(
                "INSERT INTO ai_audit_source_snapshots"
                "(snapshot_sha256, exam_id, snapshot_json, created_at) VALUES (?,?,?,?)",
                ("a" * 64, "035-I-B", "{}", snapshot_time),
            )
            conn.execute(
                "INSERT INTO ai_audit_runs"
                "(id, exam_id, snapshot_sha256, contract_version, label, created_at) "
                "VALUES (?,?,?,?,?,?)",
                ("run-gemini", "035-I-B", "a" * 64, "ai-audit-35-v1",
                 "gemini-full-audit-70", run_time),
            )
            conn.execute(
                "INSERT INTO ai_audit_passes"
                "(id, run_id, pass_number, auditor_id, model_id, prompt_version, "
                "perspective, blind, input_sha256, input_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("pass-gemini", "run-gemini", 1, "gemini-independent", "gemini-test-model",
                 "audit-prompts-1", "blind", 1, "b" * 64, "{}", pass_time),
            )
            gemini_result = {"verdicts": [
                {"subject_id": subject_id, "verdict": "finding" if i == 0 else "clear",
                 "rationale": "그대로 보존: 원본 근거", "confidence": 0.82}
                for i, subject_id in enumerate(question_ids)
            ]}
            conn.execute(
                "INSERT INTO ai_audit_results"
                "(pass_id, result_sha256, completed_subject_ids_json, notes_json, raw_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                ("pass-gemini", "c" * 64, json.dumps(question_ids), "[]",
                 json.dumps(gemini_result, ensure_ascii=False), result_time),
            )
            conn.commit()

        archive = Path(self.sandbox.name) / "third-pass.json"
        chatgpt_result = {
            "contract_version": "ai-audit-35-v1", "model": "gpt-6",
            "auditor": "chatgpt-independent",
            "results": [
                {"exam_number": number, "verdict": "clear",
                 "summary": "원문 유지", "detail": "세부 내용은 변형하지 않음"}
                for number in range(1, 71)
            ],
        }
        archive.write_text(json.dumps(chatgpt_result, ensure_ascii=False), encoding="utf-8")
        history_before = self._history(self.listening_id)
        with patch.object(review_ui, "THIRD_PASS_AUDIT_PATH", archive):
            comparison = self.store.get_independent_audit_comparison()
        self.assertEqual(len(comparison["questions"]), 70)
        sources = comparison["sources"]
        self.assertEqual(sources["gemini"]["run_id"], "run-gemini")
        self.assertEqual(sources["gemini"]["pass_id"], "pass-gemini")
        self.assertEqual(sources["gemini"]["run_created_at"], run_time)
        self.assertEqual(sources["gemini"]["pass_created_at"], pass_time)
        self.assertEqual(sources["gemini"]["result_created_at"], result_time)
        self.assertEqual(sources["gemini"]["source_snapshot_created_at"], snapshot_time)
        self.assertEqual(sources["gemini"]["source_snapshot_sha256"], "a" * 64)
        self.assertEqual(sources["gemini"]["input_sha256"], "b" * 64)
        self.assertEqual(sources["gemini"]["result_sha256"], "c" * 64)
        self.assertTrue(sources["gemini"]["timestamp_verified"])
        gemini = comparison["questions"]["1"]["gemini"]
        self.assertEqual(gemini["created_at"], result_time)
        self.assertEqual(gemini["snapshot_created_at"], snapshot_time)
        self.assertTrue(gemini["timestamp_verified"])
        self.assertTrue(gemini["snapshot_timestamp_verified"])
        self.assertEqual(gemini["summary"], "그대로 보존: 원본 근거")
        self.assertEqual(gemini["verdict"], "finding")
        chatgpt = comparison["questions"]["1"]["chatgpt"]
        self.assertEqual(chatgpt["created_at"], "unknown")
        self.assertEqual(chatgpt["snapshot_created_at"], "unknown")
        self.assertFalse(chatgpt["timestamp_verified"])
        self.assertFalse(chatgpt["snapshot_timestamp_verified"])
        self.assertEqual(chatgpt["summary"], "원문 유지")
        self.assertEqual(chatgpt["detail"], "세부 내용은 변형하지 않음")
        self.assertEqual(sources["chatgpt"]["created_at"], "unknown")
        self.assertFalse(sources["chatgpt"]["timestamp_verified"])
        self.assertEqual(sources["chatgpt"]["source_sha256"], _digest(archive))
        self.assertEqual(comparison["disagreement_count"], 1)
        self.assertEqual(self._history(self.listening_id), history_before)

    def test_independent_timestamp_only_trusts_explicit_timezone_aware_source(self):
        for value, expected in (
            ("2026-10-08T12:00:00+00:00", True),
            ("2026-10-08T21:00:00Z", True),
            ("2026-10-08T21:00:00", False),
            ("unknown", False),
            (None, False),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.store._audit_timestamp_verified(value), expected)

    def test_questions_bundle_covers_all_seventy_questions(self):
        bundle = self.store.get_questions_bundle()
        self.assertIsInstance(bundle, dict)
        self.assertEqual(bundle["exam_id"], "035-I-B")
        self.assertEqual(bundle["total_questions"], 70)
        self.assertEqual(len(bundle["questions"]), 70)
        self.assertIn(self.listening_id, bundle["questions"])
        self.assertIn(self.reading_id, bundle["questions"])
        single = self.store.get_question(self.listening_id)
        cached = bundle["questions"][self.listening_id]
        for key in ("id", "number", "section", "stem", "points", "review_status"):
            self.assertEqual(cached[key], single[key])
        self.assertEqual(len(cached["choices"]), 4)

    def test_review_ui_gracefully_ignores_database_without_ai_audit_tables(self):
        qid = self.listening_id
        version_before = self.store.get_question(qid)["version"]
        history_before = self._history(qid)
        with closing(sqlite3.connect(self.db_path)) as conn:
            for table in (
                "ai_audit_finding_occurrences", "ai_audit_findings", "ai_audit_results",
                "ai_audit_attempts", "ai_audit_checkpoints", "ai_audit_passes", "ai_audit_runs",
                "ai_audit_source_snapshots",
            ):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.commit()
        reopened = review_ui.ReviewStore(self.db_path, root=ROOT)
        listing = reopened.list_questions()
        self.assertFalse(listing["ai_audit_available"])
        self.assertTrue(all("ai_audit" not in item for item in listing["items"]))
        detail = reopened.get_question(qid)
        self.assertNotIn("ai_audit", detail)
        self.assertEqual(detail["version"], version_before)
        self.assertEqual(self._history(qid), history_before)

    def test_ai_audit_summary_is_read_only_and_separate_from_human_review_version(self):
        qid = self.listening_id
        version_before = self.store.get_question(qid)["version"]
        history_before = self._history(qid)
        with closing(sqlite3.connect(self.db_path)) as conn:
            for table in review_ui.AI_AUDIT_TABLES:
                conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (placeholder INTEGER)")
            conn.commit()

        summary = {
            "run_id": "run-3",
            "total": 3, "clear": 1, "finding": 1, "uncertain": 1,
            "unresolved_findings": 1,
            "disagreement": True,
            "risk_score": 86, "risk_level": "high",
            "convergence": "changed",
            "findings": [{"fingerprint": "f" * 64, "severity": "high",
                          "summary": "정답 연결 확인 필요"}],
            "latest_run": {"id": "run-3", "label": "third audit",
                           "created_at": "2026-10-05T02:03:00Z",
                           "pass_total": 3, "completed_passes": 3,
                           "summary": "3/3 passes completed"},
            "entries": [
                {"pass_id": "pass-a", "auditor": "auditor-a", "verdict": "finding",
                 "rationale": "정답 연결 확인 필요", "created_at": "2026-10-05T02:03:00Z"}
            ],
        }
        fake_module = SimpleNamespace(
            summarize_all_questions=MagicMock(return_value={qid: summary}),
            summarize_question=MagicMock(return_value=summary),
        )
        with patch.dict(sys.modules, {"src.ai_audit_35": fake_module}):
            listing = self.store.list_questions()
            detail = self.store.get_question(qid)

        self.assertTrue(listing["ai_audit_available"])
        listed = next(item for item in listing["items"] if item["id"] == qid)
        self.assertEqual(listed["ai_audit"]["total"], 3)
        self.assertEqual(listed["ai_audit"]["clear"], 1)
        self.assertEqual(listed["ai_audit"]["finding"], 1)
        self.assertEqual(listed["ai_audit"]["uncertain"], 1)
        self.assertEqual(listed["ai_audit"]["unresolved_findings"], 1)
        self.assertEqual(listed["ai_audit"]["risk_score"], 86)
        self.assertEqual(detail["ai_audit"]["entries"][0]["auditor"], "auditor-a")
        self.assertEqual(detail["ai_audit"]["latest_run"]["id"], "run-3")
        self.assertNotIn("execution", detail["ai_audit"])
        self.assertNotIn("attempt_total", listed["ai_audit"])
        self.assertEqual(detail["version"], version_before)
        self.assertEqual(self._history(qid), history_before)
        fake_module.summarize_all_questions.assert_called_once()
        fake_module.summarize_question.assert_called_once()
        self.assertEqual(fake_module.summarize_question.call_args.args[1], qid)

    def test_ai_audit_positive_summary_refreshes_on_each_detail_read(self):
        qid = self.listening_id
        fake_module = MagicMock(
            summarize_question=MagicMock(side_effect=[
                {"run_id": "run-1", "verdict": "clear", "total": 1},
                {"run_id": "run-2", "verdict": "clear", "total": 1},
                {"run_id": "run-3", "verdict": "clear", "total": 1},
            ]),
            status_report=MagicMock(return_value={"latest_run": {"attempt_status_counts": {}}}),
            question_audit_history=MagicMock(return_value={"runs": []}),
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            for table in review_ui.AI_AUDIT_TABLES:
                conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (placeholder INTEGER)")
            conn.commit()

        with patch.dict(sys.modules, {"src.ai_audit_35": fake_module}):
            first = self.store.get_question(qid)
            self.assertEqual(fake_module.summarize_question.call_count, 1)

            # Positive append-only audit state can change while the reviewer is
            # running, so explicit detail reads must refresh rather than reuse a
            # process-lifetime positive cache entry.
            second = self.store.get_question(qid)
            self.assertEqual(fake_module.summarize_question.call_count, 2)
            self.assertNotEqual(first["ai_audit"]["run_id"], second["ai_audit"]["run_id"])

            third = self.store.get_question(qid)
            self.assertEqual(fake_module.summarize_question.call_count, 3)
            self.assertEqual(third["ai_audit"]["run_id"], "run-3")

    def test_ai_audit_detail_can_appear_after_initial_no_audit_read(self):
        qid = self.listening_id
        fake_module = MagicMock(
            summarize_question=MagicMock(side_effect=[
                {},
                {"run_id": "run-new", "verdict": "finding", "total": 1},
            ]),
            status_report=MagicMock(return_value={"latest_run": None, "passes": []}),
            question_audit_history=MagicMock(return_value={"runs": []}),
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            for table in review_ui.AI_AUDIT_TABLES:
                conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (placeholder INTEGER)")
            conn.commit()

        with patch.dict(sys.modules, {"src.ai_audit_35": fake_module}):
            first = self.store.get_question(qid)
            second = self.store.get_question(qid)
        self.assertNotIn("ai_audit", first)
        self.assertEqual(second["ai_audit"]["run_id"], "run-new")
        self.assertEqual(fake_module.summarize_question.call_count, 2)

    def test_real_ai_audit_helper_flows_into_list_and_detail_without_human_state_change(self):
        qid = self.listening_id
        version_before = self.store.get_question(qid)["version"]
        history_before = self._history(qid)
        status_before = self._row(qid)[1]

        run = ai_audit_35.create_run(
            self.db_path,
            auditors=["ui-integration-auditor"],
            model_id="ui-integration-model",
            prompt_version="ui-integration-v1",
            perspective="independent",
            label="review-ui-integration",
        )
        audit_pass = run["passes"][0]
        exported = ai_audit_35.export_pass(self.db_path, pass_id=audit_pass["id"])
        subject_ids = [item["id"] for item in exported["source"]["questions"]]
        verdicts = [
            {
                "subject_id": subject_id,
                "verdict": "finding" if subject_id == qid else "clear",
                "confidence": 0.9,
                "rationale": "UI integration fixture",
            }
            for subject_id in subject_ids
        ]
        result_payload = {
            "schema_version": ai_audit_35.RESULT_SCHEMA_VERSION,
            "kind": "final",
            "pass_id": audit_pass["id"],
            "input_sha256": exported["input_sha256"],
            "snapshot_sha256": exported["snapshot_sha256"],
            "completed_subject_ids": subject_ids,
            "verdicts": verdicts,
            "findings": [{
                "subject_id": qid,
                "category": "ui.integration",
                "severity": "high",
                "summary": "사람 검수에서 다시 확인할 항목",
                "detail": "실제 AI summary helper와 ReviewStore 연결 검증",
                "evidence": {"field": "answer.choice_number", "fixture": True},
            }],
            "notes": ["review UI integration fixture"],
            "state": {},
        }
        ai_audit_35.record_attempt_outcome(
            self.db_path, audit_pass["id"], "failed",
            error_code="provider_error", error_message="provider returned 503",
            evidence={"http_status": 503}, response={"provider": "unavailable"},
        )
        ai_audit_35.record_attempt_outcome(
            self.db_path, audit_pass["id"], "timed_out",
            error_code="deadline_exceeded", error_message="response exceeded 120 seconds",
            evidence={"timeout_seconds": 120},
        )
        invalid_payload = dict(result_payload)
        invalid_payload["verdicts"] = []
        invalid = ai_audit_35.ingest_response(
            self.db_path, audit_pass["id"], invalid_payload,
            evidence={"transport": "ui-integration-invalid"},
        )
        self.assertEqual(invalid["status"], "invalid")
        succeeded = ai_audit_35.ingest_response(
            self.db_path, audit_pass["id"], result_payload,
            evidence={"transport": "ui-integration-success"},
        )
        self.assertEqual(succeeded["status"], "succeeded")

        listing = self.store.list_questions()
        listed = next(item for item in listing["items"] if item["id"] == qid)
        detail = self.store.get_question(qid)
        backend_summary = ai_audit_35.summarize_question(self.db_path, qid)
        self.assertTrue(listing["ai_audit_available"])
        self.assertEqual(listed["ai_audit"]["total"], 1)
        self.assertEqual(listed["ai_audit"]["finding"], 1)
        self.assertEqual(listed["ai_audit"]["unresolved_findings"], 1)
        self.assertEqual(listed["ai_audit"]["risk_score"], backend_summary["risk_score"])
        self.assertEqual(listed["ai_audit"]["risk_level"], backend_summary["risk_level"])
        self.assertEqual(listed["ai_audit"]["convergence"], backend_summary["convergence"])
        self.assertNotIn("entries", listed["ai_audit"])
        self.assertNotIn("execution", listed["ai_audit"])
        self.assertEqual(listed["ai_audit"]["attempt_total"], 4)
        self.assertEqual(listed["ai_audit"]["attempt_status_counts"], {
            "succeeded": 1, "failed": 1, "timed_out": 1, "invalid": 1,
        })
        self.assertEqual(listed["ai_audit"]["retry_count"], 3)
        self.assertTrue(listed["ai_audit"]["has_partial_failures"])
        self.assertEqual(detail["ai_audit"]["entries"][0]["auditor_id"], "ui-integration-auditor")
        self.assertEqual(detail["ai_audit"]["entries"][0]["model_id"], "ui-integration-model")
        self.assertEqual(detail["ai_audit"]["entries"][0]["prompt_version"], "ui-integration-v1")
        finding = detail["ai_audit"]["entries"][0]["findings"][0]
        self.assertEqual(finding["evidence"], {"field": "answer.choice_number", "fixture": True})
        self.assertEqual(finding["identity"], {"field": "answer.choice_number", "fixture": True})
        execution = detail["ai_audit"]["execution"]
        self.assertEqual(execution["attempt_total"], 4)
        self.assertEqual(execution["retry_count"], 3)
        self.assertEqual(execution["incomplete_passes"], 0)
        attempts = execution["passes"][0]["attempts"]
        self.assertEqual(
            [(item["attempt_number"], item["status"]) for item in attempts],
            [(1, "failed"), (2, "timed_out"), (3, "invalid"), (4, "succeeded")],
        )
        self.assertEqual(attempts[0]["error_code"], "provider_error")
        self.assertEqual(attempts[0]["evidence"], {"http_status": 503})
        self.assertEqual(attempts[0]["raw_response"], {"provider": "unavailable"})
        self.assertEqual(attempts[1]["evidence"], {"timeout_seconds": 120})
        self.assertEqual(attempts[2]["evidence"], {"transport": "ui-integration-invalid"})
        self.assertEqual(attempts[3]["evidence"], {"transport": "ui-integration-success"})
        self.assertEqual(detail["version"], version_before)
        self.assertEqual(self._row(qid)[1], status_before)
        self.assertEqual(self._history(qid), history_before)

    def test_ai_attempt_execution_is_scoped_to_questions_present_in_the_pass(self):
        listening_id = self.listening_id
        reading_id = self.reading_id
        listening_version = self.store.get_question(listening_id)["version"]
        reading_version = self.store.get_question(reading_id)["version"]
        listening_history = self._history(listening_id)
        reading_history = self._history(reading_id)
        listening_status = self._row(listening_id)[1]
        reading_status = self._row(reading_id)[1]

        run = ai_audit_35.create_run(
            self.db_path,
            auditors=["ui-transcript-only-auditor"],
            model_id="ui-transcript-only-model",
            prompt_version="ui-transcript-only-v1",
            perspective="transcript_alignment",
            label="review-ui-subject-scope",
        )
        audit_pass = run["passes"][0]
        exported = ai_audit_35.export_pass(self.db_path, pass_id=audit_pass["id"])
        self.assertTrue(exported["subjects"])
        self.assertTrue(all(item["section"] == "listening" for item in exported["subjects"]))
        self.assertNotIn(reading_id, {item["id"] for item in exported["subjects"]})
        ai_audit_35.record_attempt_outcome(
            self.db_path, audit_pass["id"], "timed_out",
            error_code="deadline_exceeded", error_message="transcript-only timeout",
            evidence={"timeout_seconds": 90, "scope": "listening-only"},
        )

        listing = self.store.list_questions()
        listening_item = next(item for item in listing["items"] if item["id"] == listening_id)
        reading_item = next(item for item in listing["items"] if item["id"] == reading_id)
        self.assertEqual(listening_item["ai_audit"]["attempt_total"], 1)
        self.assertEqual(listening_item["ai_audit"]["attempt_status_counts"]["timed_out"], 1)
        self.assertEqual(listening_item["ai_audit"]["incomplete_passes"], 1)
        self.assertNotIn("ai_audit", reading_item)

        listening_detail = self.store.get_question(listening_id)
        reading_detail = self.store.get_question(reading_id)
        self.assertEqual(listening_detail["ai_audit"]["execution"]["passes"][0]["attempts"][0]["status"], "timed_out")
        self.assertEqual(
            listening_detail["ai_audit"]["execution"]["passes"][0]["attempts"][0]["evidence"],
            {"timeout_seconds": 90, "scope": "listening-only"},
        )
        self.assertNotIn("ai_audit", reading_detail)
        self.assertEqual(self.store.get_question(listening_id)["version"], listening_version)
        self.assertEqual(self.store.get_question(reading_id)["version"], reading_version)
        self.assertEqual(self._row(listening_id)[1], listening_status)
        self.assertEqual(self._row(reading_id)[1], reading_status)
        self.assertEqual(self._history(listening_id), listening_history)
        self.assertEqual(self._history(reading_id), reading_history)

    def test_existing_ai_verdicts_remain_visible_without_attempt_table(self):
        qid = self.reading_id
        version_before = self.store.get_question(qid)["version"]
        history_before = self._history(qid)
        status_before = self._row(qid)[1]

        run = ai_audit_35.create_run(
            self.db_path,
            auditors=["legacy-no-attempt-auditor"],
            model_id="legacy-no-attempt-model",
            prompt_version="legacy-no-attempt-v1",
            perspective="independent",
            label="review-ui-legacy-no-attempt",
        )
        audit_pass = run["passes"][0]
        exported = ai_audit_35.export_pass(self.db_path, pass_id=audit_pass["id"])
        subject_ids = [item["id"] for item in exported["subjects"]]
        response = {
            "schema_version": ai_audit_35.RESULT_SCHEMA_VERSION,
            "kind": "final",
            "pass_id": audit_pass["id"],
            "input_sha256": exported["input_sha256"],
            "snapshot_sha256": exported["snapshot_sha256"],
            "completed_subject_ids": subject_ids,
            "verdicts": [{
                "subject_id": subject_id,
                "verdict": "clear",
                "confidence": 0.9,
                "rationale": "legacy compatibility fixture",
            } for subject_id in subject_ids],
            "findings": [],
            "notes": ["legacy no-attempt table fixture"],
            "state": {},
        }
        self.assertEqual(
            ai_audit_35.ingest_response(self.db_path, audit_pass["id"], response)["status"],
            "succeeded",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DROP TABLE ai_audit_attempts")
            conn.commit()

        listing = self.store.list_questions()
        listed = next(item for item in listing["items"] if item["id"] == qid)
        detail = self.store.get_question(qid)
        self.assertEqual(listed["ai_audit"]["total"], 1)
        self.assertEqual(listed["ai_audit"]["clear"], 1)
        self.assertEqual(listed["ai_audit"]["attempt_total"], 0)
        self.assertEqual(detail["ai_audit"]["total"], 1)
        self.assertEqual(detail["ai_audit"]["execution"]["attempt_total"], 0)
        self.assertEqual(detail["ai_audit"]["execution"]["passes"][0]["attempts"], [])
        self.assertEqual(detail["version"], version_before)
        self.assertEqual(self._row(qid)[1], status_before)
        self.assertEqual(self._history(qid), history_before)

    def test_multi_run_ai_history_is_queryable_after_reopen_without_changing_human_state(self):
        qid = self.reading_id
        version_before = self.store.get_question(qid)["version"]
        history_before = self._history(qid)
        status_before = self._row(qid)[1]
        baseline_ai = ai_audit_35.question_audit_history(self.db_path, qid)

        def result_for(pass_info, verdict, *, finding=False):
            exported = ai_audit_35.export_pass(self.db_path, pass_id=pass_info["id"])
            self.assertEqual([item["id"] for item in exported["subjects"]], [qid])
            findings = []
            if finding:
                findings.append({
                    "subject_id": qid,
                    "category": "history.answer",
                    "severity": "medium",
                    "summary": "과거 실행에서 확인된 답안 매핑",
                    "detail": "다음 실행에서는 재현되지 않을 수 있지만 해결 판정은 아님",
                    "evidence": {"field": "answer.choice_number", "run_fixture": True},
                })
            return {
                "schema_version": ai_audit_35.RESULT_SCHEMA_VERSION,
                "kind": "final",
                "pass_id": pass_info["id"],
                "input_sha256": exported["input_sha256"],
                "snapshot_sha256": exported["snapshot_sha256"],
                "completed_subject_ids": [qid],
                "verdicts": [{
                    "subject_id": qid,
                    "verdict": verdict,
                    "confidence": 0.92,
                    "rationale": f"history fixture {verdict}",
                }],
                "findings": findings,
                "notes": ["review UI history fixture"],
                "state": {},
            }

        first = ai_audit_35.create_run(
            self.db_path,
            auditors=["history-first-a", "history-first-b"],
            model_id="history-model",
            prompt_version="history-v1",
            perspective="independent",
            subject_ids=[qid],
            label="older completed audit",
        )
        ai_audit_35.ingest_response(
            self.db_path, first["passes"][0]["id"],
            result_for(first["passes"][0], "finding", finding=True),
            evidence={"run": 1, "pass": 1},
        )
        ai_audit_35.ingest_response(
            self.db_path, first["passes"][1]["id"],
            result_for(first["passes"][1], "clear"),
            evidence={"run": 1, "pass": 2},
        )
        second = ai_audit_35.create_run(
            self.db_path,
            auditors=["history-second-a"],
            model_id="history-model",
            prompt_version="history-v2",
            perspective="independent",
            subject_ids=[qid],
            label="newer completed audit",
        )
        ai_audit_35.ingest_response(
            self.db_path, second["passes"][0]["id"],
            result_for(second["passes"][0], "clear"),
            evidence={"run": 2, "pass": 1},
        )

        detail = self.store.get_question(qid)
        audit_history = detail["ai_audit"]["history"]
        self.assertEqual(audit_history["run_count"], baseline_ai.get("run_count", 0) + 2)
        self.assertEqual(audit_history["audit_count"], baseline_ai.get("audit_count", 0) + 3)
        baseline_counts = baseline_ai.get("verdict_counts", {})
        self.assertEqual(audit_history["verdict_counts"]["clear"], baseline_counts.get("clear", 0) + 2)
        self.assertEqual(audit_history["verdict_counts"]["finding"], baseline_counts.get("finding", 0) + 1)
        self.assertEqual(audit_history["verdict_counts"]["uncertain"], baseline_counts.get("uncertain", 0))
        self.assertEqual(
            audit_history["disagreement_run_count"],
            baseline_ai.get("disagreement_run_count", 0) + 1,
        )
        self.assertEqual([item["label"] for item in audit_history["runs"][:2]], [
            "newer completed audit", "older completed audit",
        ])
        self.assertFalse(audit_history["runs"][0]["summary"]["disagreement"])
        self.assertTrue(audit_history["runs"][1]["summary"]["disagreement"])
        self.assertEqual(audit_history["runs"][1]["summary"]["finding"], 1)
        fixture_finding = next(
            item for item in audit_history["historical_findings"]
            if item["summary"] == "과거 실행에서 확인된 답안 매핑"
        )
        self.assertEqual(
            fixture_finding["latest_state"],
            "not_reproduced_in_latest_run",
        )

        reopened = review_ui.ReviewStore(self.db_path, root=ROOT)
        reopened_history = reopened.get_question(qid)["ai_audit"]["history"]
        self.assertEqual(reopened_history["audit_count"], baseline_ai.get("audit_count", 0) + 3)
        self.assertEqual(reopened_history["runs"][1]["label"], "older completed audit")
        self.assertEqual(reopened.get_question(qid)["version"], version_before)
        self.assertEqual(self._row(qid)[1], status_before)
        self.assertEqual(self._history(qid), history_before)

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

    def test_changed_source_pdf_cannot_be_served_or_used_to_approve(self):
        # Reproduce source drift with a private copy. Never change the PDF
        # belonging to the user's real corpus.
        with closing(sqlite3.connect(self.db_path)) as conn:
            relative = conn.execute(
                "SELECT s.relative_path FROM questions q JOIN source_files s "
                "ON s.id=q.source_file_id WHERE q.id=?", (self.listening_id,),
            ).fetchone()[0]
        isolated_root = Path(self.sandbox.name) / "isolated-corpus"
        paper = isolated_root / relative
        paper.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / relative, paper)
        self.assertEqual(paper.stat().st_size, (ROOT / relative).stat().st_size)
        with paper.open("r+b") as source:
            source.seek(16)
            previous = source.read(1)
            source.seek(16)
            source.write(bytes([previous[0] ^ 1]))
        isolated_store = review_ui.ReviewStore(self.db_path, root=isolated_root)
        payload = self._payload(self.listening_id)
        baseline = (self._row(self.listening_id), self._history(self.listening_id))
        with self.assertRaisesRegex(review_ui.Conflict, "checksum changed"):
            isolated_store.media_path(self.listening_id, "paper")
        with self.assertRaisesRegex(review_ui.Conflict, "checksum changed"):
            isolated_store.save_review(self.listening_id, payload)
        self.assertEqual((self._row(self.listening_id), self._history(self.listening_id)), baseline)


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
        self.server = review_ui.ThreadingHTTPServer(("127.0.0.1", 0), review_ui.make_handler(self.store, access_key=None, allow_unauthenticated_test_fixture=True))
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
        code, _, _ = self._request("GET", "/api/questions", headers={"Host": "evil.example"})
        self.assertEqual(code, 403)

    def test_get_questions_bundle_endpoint(self):
        bundle, headers = self._get_json("/api/questions-bundle")
        self.assertEqual(bundle["exam_id"], "035-I-B")
        self.assertEqual(bundle["total_questions"], 70)
        self.assertEqual(len(bundle["questions"]), 70)
        self.assertEqual(headers["Cache-Control"], "no-store")

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
