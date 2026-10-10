"""F3 single-flight and stale-safe cache under concurrent read-only HTTP polls."""

import sqlite3
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from src.review_ui import ReviewStore
from tests import test_ai_audit_35 as fixtures


class AuditSummaryCacheTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.AIAudit35Tests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.path = fixture.db_path
        self.store = ReviewStore(db_path=self.path)
        self.store.backend = "postgres"  # Exercise asynchronous PG path with a local disposable fixture.
        self.store.database_url = "mock-pg://read-only-fixture"

    def test_ten_gets_share_one_job_and_no_review_rows_in_response(self):
        gate = threading.Event()
        begun = threading.Event()
        counts = []

        def compute():
            counts.append(1)
            begun.set()
            self.assertTrue(gate.wait(timeout=5))
            return True, {fixtures.Q1: {"risk_score": 65, "run_id": "r1"}}

        with patch.object(self.store, "_compute_ai_list_summary", side_effect=compute):
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = [pool.submit(self.store.list_ai_audit_summary) for _ in range(10)]
                self.assertTrue(begun.wait(2))
                results = [task.result(timeout=3) for task in futures]
            self.assertTrue(all(row["state"] == "loading" for row in results))
            self.assertEqual(len(counts), 1)
            gate.set()
            self.assertTrue(self._wait_until(lambda: not self.store._ai_summaries_loading))
            response = self.store.list_ai_audit_summary()
            self.assertEqual(response["state"], "ready")
            self.assertEqual(response["items"], [
                {"id": fixtures.Q1, "ai_audit": {"risk_score": 65, "run_id": "r1"}}
            ])
            self.assertEqual(len(counts), 1)
            self.assertFalse(any("review_version" in entry or "status" in entry for entry in response["items"]))

    def test_cache_age_refresh_keeps_previous_success_until_new_version(self):
        self.store._ai_summaries_cache = (True, {fixtures.Q1: {"risk_score": 35}})
        self.store._ai_summaries_checked_at = time.monotonic() - 100
        gate = threading.Event()

        def compute():
            self.assertTrue(gate.wait(5))
            return True, {fixtures.Q1: {"risk_score": 90}}

        with patch.object(self.store, "_compute_ai_list_summary", side_effect=compute):
            stale = self.store.list_ai_audit_summary()
            self.assertEqual(stale["state"], "ready")
            self.assertEqual(stale["items"][0]["ai_audit"]["risk_score"], 35)
            gate.set()
            self.assertTrue(self._wait_until(lambda: not self.store._ai_summaries_loading))
        fresh = self.store.list_ai_audit_summary()
        self.assertEqual(fresh["items"][0]["ai_audit"]["risk_score"], 90)

    def test_initial_error_is_distinct_from_unavailable_and_can_retry(self):
        attempts = []

        def compute():
            attempts.append(1)
            if len(attempts) == 1:
                raise TimeoutError("intentional fixture failure")
            return False, {}

        with patch.object(self.store, "_compute_ai_list_summary", side_effect=compute):
            first = self.store.list_ai_audit_summary()
            self.assertIn(first["state"], ("loading", "error"))
            self.assertTrue(self._wait_until(lambda: not self.store._ai_summaries_loading))
            self.assertEqual(self.store.list_ai_audit_summary()["state"], "error")
            self.assertEqual(len(attempts), 1)
            self.store._ai_summaries_checked_at = time.monotonic() - 11
            self.store.list_ai_audit_summary()
            self.assertTrue(self._wait_until(lambda: len(attempts) == 2 and not self.store._ai_summaries_loading))
            self.assertEqual(self.store.list_ai_audit_summary()["state"], "unavailable")

    def test_postgres_legacy_list_does_not_call_per_question_audit(self):
        self.store._ai_summaries_cache = (True, {fixtures.Q1: {"risk_score": 35}})
        self.store._ai_summaries_checked_at = time.monotonic()

        def read_db(*args, **kwargs):
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            return connection

        with (patch.object(self.store, "_connect", side_effect=read_db),
              patch.object(self.store, "_ai_audit_execution", side_effect=AssertionError("N+1 execution")),
              patch.object(self.store, "_ai_audit_summaries", side_effect=AssertionError("N+1 summary"))):
            listing = self.store.list_questions()
        self.assertEqual(len(listing["items"]), 2)
        self.assertTrue(listing["ai_audit_available"])
        self.assertEqual(listing["items"][0]["ai_audit"]["risk_score"], 35)

    @staticmethod
    def _wait_until(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(.01)
        return predicate()


if __name__ == "__main__":
    unittest.main()
