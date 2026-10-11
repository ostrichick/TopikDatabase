"""P2 reviewer leases: never share transaction state or salvage broken sockets.

These tests operate on fake connections; they never access the operational DB.
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import database


class FakeRaw:
    def __init__(self, *, readonly=False):
        self.read_only = readonly
        self.info = SimpleNamespace(transaction_status=SimpleNamespace(name="IDLE"))
        self.rollbacks = 0
        self.closed = False

    def rollback(self):
        self.rollbacks += 1
        self.info.transaction_status.name = "IDLE"

    def close(self):
        self.closed = True


class FakePool:
    def __init__(self, raw):
        self.raw = raw
        self.retired = []

    def getconn(self):
        return self.raw

    def putconn(self, raw):
        self.retired.append(raw)


class ReviewerPoolTests(unittest.TestCase):
    def test_read_lease_always_rolls_back_even_if_uncommitted(self):
        raw = FakeRaw(readonly=True)
        pool = FakePool(raw)
        with patch.object(database, "_review_pool", return_value=pool):
            conn = database.PostgresReadConnection("postgresql://fixture", pooled=True)
            raw.info.transaction_status.name = "INTRANS"
            conn.close()
            conn.close()  # idempotent; do not double-return lease
        self.assertEqual(raw.rollbacks, 1)
        self.assertEqual(pool.retired, [raw])
        self.assertEqual(raw.info.transaction_status.name, "IDLE")

    def test_write_lease_rolls_back_after_failed_review(self):
        raw = FakeRaw(readonly=False)
        pool = FakePool(raw)
        with patch.object(database, "_review_pool", return_value=pool):
            conn = database.PostgresWriteConnection("postgresql://fixture", pooled=True)
            raw.info.transaction_status.name = "INERROR"
            conn.close()
        self.assertEqual(raw.rollbacks, 1)
        self.assertFalse(raw.closed)
        self.assertEqual(pool.retired, [raw])

    def test_unsafe_lease_is_closed_not_shared(self):
        for readonly, leaked in ((True, False), (False, True)):
            with self.subTest(readonly=readonly):
                raw = FakeRaw(readonly=leaked)
                pool = FakePool(raw)
                with patch.object(database, "_review_pool", return_value=pool):
                    with self.assertRaises(database.DatabaseOperationError):
                        database._reviewer_lease("postgresql://fixture", readonly=readonly, pooled=True)
                self.assertTrue(raw.closed)
                self.assertEqual(pool.retired, [raw])

    def test_broken_lease_discarded_after_rollback_failure(self):
        raw = FakeRaw(readonly=True)
        pool = FakePool(raw)
        def broken_rollback():
            raise ConnectionError("SSH tunnel disconnected")
        raw.rollback = broken_rollback
        with patch.object(database, "_review_pool", return_value=pool):
            conn = database.PostgresReadConnection("postgresql://fixture", pooled=True)
            conn.close()
        self.assertTrue(raw.closed)
        self.assertEqual(pool.retired, [raw])

    def test_pool_disabled_falls_back_to_unpooled(self):
        raw = FakeRaw(readonly=True)
        with patch.dict(os.environ, {"TOPIK_REVIEW_POOL": "off"}):
            with patch.object(database, "connect_postgres", return_value=raw) as connect:
                conn = database.PostgresReadConnection("postgresql://fixture", pooled=True)
                conn.close()
        connect.assert_called_once_with("postgresql://fixture", readonly=True)
        self.assertTrue(raw.closed)
        self.assertEqual(raw.rollbacks, 0)


if __name__ == "__main__":
    unittest.main()
