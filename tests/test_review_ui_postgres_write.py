"""Stage-6 PostgreSQL write/transaction contract tests without a live server."""

from __future__ import annotations

import unittest
import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from src import database, review_ui


class TransactionProbe:
    def __init__(self, fail=None):
        self.fail = fail
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if self.fail is not None:
            error = self.fail
            self.fail = None
            raise error
        return self

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


class Stage6TransactionTests(unittest.TestCase):
    def _store(self):
        store = object.__new__(review_ui.ReviewStore)
        store.backend = "postgres"
        store.database_url = "postgresql://fixture.invalid/topik"
        return store

    def test_postgres_transaction_commits_once(self):
        store = self._store()
        probe = TransactionProbe()
        with patch.object(store, "_connect", return_value=probe):
            with store._write_transaction() as db:
                self.assertIs(db, probe)
        self.assertEqual(probe.commits, 1)
        self.assertEqual(probe.rollbacks, 0)
        self.assertEqual(probe.closed, 1)

    def test_database_failure_rolls_back_once_without_retry(self):
        store = self._store()
        failure = database.DatabaseOperationError("simulated deadlock/serialization failure")
        probe = TransactionProbe(fail=failure)
        with patch.object(store, "_connect", return_value=probe):
            with self.assertRaises(database.DatabaseOperationError):
                with store._write_transaction() as db:
                    db.execute("UPDATE questions SET stem=? WHERE id=?", ("x", "q"))
        self.assertEqual(len(probe.calls), 1, "Stage 6 must never auto-retry a failed write")
        self.assertEqual(probe.commits, 0)
        self.assertEqual(probe.rollbacks, 1)
        self.assertEqual(probe.closed, 1)

    def test_review_preflight_reuses_one_writable_session_and_resets_snapshot(self):
        store = self._store()
        probe = TransactionProbe()
        events = []
        original_rollback = probe.rollback
        original_commit = probe.commit
        original_close = probe.close

        def rollback():
            events.append("rollback")
            original_rollback()

        def commit():
            events.append("commit")
            original_commit()

        def close():
            events.append("close")
            original_close()

        probe.rollback, probe.commit, probe.close = rollback, commit, close
        def validate(db, question_id):
            self.assertIs(db, probe)
            self.assertEqual(question_id, "036-I-R-001")
            events.append("preflight")

        with patch.object(store, "_connect", return_value=probe) as connect, \
             patch.object(store, "_verify_review_media", side_effect=validate):
            with store._review_write_transaction("036-I-R-001") as db:
                self.assertIs(db, probe)
                events.append("write")
        connect.assert_called_once_with(writable=True)
        self.assertEqual(events, ["preflight", "rollback", "write", "commit", "close"])
        self.assertEqual((probe.rollbacks, probe.commits, probe.closed), (1, 1, 1))

    def test_review_preflight_failure_cannot_enter_write_transaction(self):
        store = self._store()
        probe = TransactionProbe()
        with patch.object(store, "_connect", return_value=probe) as connect, \
             patch.object(store, "_verify_review_media", side_effect=ValueError("tampered PDF")):
            with self.assertRaisesRegex(ValueError, "tampered PDF"):
                with store._review_write_transaction("036-I-R-001"):
                    self.fail("must never reach write phase")
        connect.assert_called_once_with(writable=True)
        self.assertEqual((probe.commits, probe.rollbacks, probe.closed), (0, 1, 1))

    def test_review_write_failure_rolls_back_after_preflight_without_retry(self):
        store = self._store()
        probe = TransactionProbe()
        with patch.object(store, "_connect", return_value=probe) as connect, \
             patch.object(store, "_verify_review_media", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "simulated write failure"):
                with store._review_write_transaction("036-I-R-001") as db:
                    db.execute("SELECT 1")
                    raise RuntimeError("simulated write failure")
        connect.assert_called_once_with(writable=True)
        self.assertEqual((probe.commits, probe.rollbacks, probe.closed), (0, 2, 1))
        self.assertEqual(len(probe.calls), 1)

    def test_postgres_question_lock_is_single_joined_select(self):
        store = self._store()
        store.question_prefix = "036-I-"
        store.exam_id = "036-I-B"

        class Cursor:
            def fetchone(self):
                return {"id": "036-I-R-001"}

        class Probe(TransactionProbe):
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Cursor()

        probe = Probe()
        row = store._lock_question(probe, "036-I-R-001")
        self.assertEqual(row["id"], "036-I-R-001")
        self.assertEqual(len(probe.calls), 1)
        self.assertIn("JOIN answers a", probe.calls[0][0])
        self.assertIn("FOR UPDATE OF q", probe.calls[0][0])
        self.assertEqual(probe.calls[0][1], ("036-I-R-001", "036-I-B"))

    def test_36_version_marker_and_review_count_use_one_pg_query(self):
        store = self._store()
        store.exam_id = "036-I-B"

        class Cursor:
            def fetchone(self):
                return {"review_count": 5}

        class Probe(TransactionProbe):
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Cursor()

        probe = Probe()
        self.assertEqual(store._version(probe, "036-I-R-001"), 5)
        self.assertEqual(len(probe.calls), 1)
        self.assertIn("EXISTS(SELECT 1 FROM import_metadata", probe.calls[0][0])
        self.assertEqual(probe.calls[0][1],
                         ("036-I-B:punctuation:v4-to-v5", "036-I-R-001"))

    def test_batched_pg_preflight_keeps_full_sha_and_exam_scope(self):
        class Cursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        class Probe(TransactionProbe):
            def __init__(self, row):
                super().__init__()
                self.row = row

            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Cursor(self.row)

        with tempfile.TemporaryDirectory(prefix="topik-pg-review-media-") as temp:
            for session, section in (("035", "reading"), ("036", "listening")):
                with self.subTest(session=session):
                    store = self._store()
                    store.exam_id = f"{session}-I-B"
                    store.question_prefix = f"{session}-I-"
                    store.media_root = (Path(temp) / "topik-past-papers").resolve()
                    store.source_root = (store.media_root / f"{int(session)}th").resolve()
                    store.source_root.mkdir(parents=True, exist_ok=True)
                    metadata = {"section": section}
                    for kind in ("paper", "answer", "transcript"):
                        payload = f"%PDF-1.4\n{session} {kind}\n%%EOF".encode()
                        relative = f"topik-past-papers/{int(session)}th/{kind}.pdf"
                        (store.source_root / f"{kind}.pdf").write_bytes(payload)
                        metadata[f"{kind}_id"] = kind
                        metadata[f"{kind}_path"] = relative
                        metadata[f"{kind}_hash"] = hashlib.sha256(payload).hexdigest()
                        metadata[f"{kind}_size"] = len(payload)
                    probe = Probe(metadata)
                    qid = f"{session}-I-{'L' if section == 'listening' else 'R'}-001"
                    store._verify_review_media(probe, qid)
                    self.assertEqual(len(probe.calls), 1, "media metadata must use one SQL request")
                    self.assertEqual(probe.calls[0][1], (qid, store.exam_id))
                    self.assertIn("JOIN answers a", probe.calls[0][0])
                    self.assertIn("LEFT JOIN transcripts t", probe.calls[0][0])
                    # Equal-byte-size tampering must still fail the full hash check.
                    source = store.source_root / "paper.pdf"
                    changed = source.read_bytes().replace(b"%PDF", b"%PDA")
                    self.assertEqual(len(changed), source.stat().st_size)
                    source.write_bytes(changed)
                    with self.assertRaisesRegex(review_ui.Conflict, "checksum changed"):
                        store._verify_review_media(probe, qid)

    def test_batched_pg_preflight_refuses_missing_listening_transcript(self):
        store = self._store()
        store.exam_id = "036-I-B"
        store.question_prefix = "036-I-"
        row = {"section": "listening", "paper_id": "paper", "answer_id": "answer",
               "transcript_id": None, "paper_path": "test.pdf", "answer_path": "test.pdf",
               "paper_hash": "unused", "answer_hash": "unused", "paper_size": 0,
               "answer_size": 0}

        class Cursor:
            def fetchone(self):
                return row

        class Probe(TransactionProbe):
            def execute(self, sql, params=()):
                return Cursor()

        # Avoid actual local files; the exact preflight validation order is
        # local hash checks first, missing transcript rejection last.
        with patch.object(store, "_verify_media_source", return_value=None):
            with self.assertRaisesRegex(review_ui.NotFound, "no such source"):
                store._verify_review_media(Probe(), "036-I-L-001")

    def test_postgres_audio_lock_order_is_deterministic(self):
        store = self._store()

        class RowsProbe(TransactionProbe):
            def execute(self, sql, params=()):
                self.calls.append((sql, tuple(params)))
                if "SELECT id FROM questions" in sql:
                    return Cursor([{"id": item} for item in params])
                if "SELECT * FROM audio_segments" in sql:
                    return Cursor([])
                return Cursor([])

        class Cursor:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

            def __iter__(self):
                return iter(self.rows)

        probe = RowsProbe()
        qids = ["035-I-L-026", "035-I-L-025"]
        store._locked_audio_rows(probe, qids)
        self.assertEqual(probe.calls[0][1], ("035-I-L-025", "035-I-L-026"))
        self.assertIn("ORDER BY id FOR UPDATE", probe.calls[0][0])
        self.assertIn("ORDER BY question_id FOR UPDATE", probe.calls[1][0])


if __name__ == "__main__":
    unittest.main()
