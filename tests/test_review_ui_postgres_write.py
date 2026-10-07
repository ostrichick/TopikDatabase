"""Stage-6 PostgreSQL write/transaction contract tests without a live server."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
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
