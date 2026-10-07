"""Offline Stage-7 PostgreSQL AI-audit adapter and transaction contracts."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from src import ai_audit_35, database


class Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


class AIAuditPostgresTests(unittest.TestCase):
    def test_audit_adapter_uses_tuple_rows_qmark_translation_and_readonly_session(self):
        class FakeError(Exception):
            pass

        class Raw:
            def __init__(self):
                self.statements = []
                self.commits = 0
                self.rollbacks = 0
                self.closed = False

            def execute(self, sql, params=()):
                self.statements.append((sql, params))
                return Cursor([("ok",)])

            def commit(self):
                self.commits += 1

            def rollback(self):
                self.rollbacks += 1

            def close(self):
                self.closed = True

        raw = Raw()

        class FakePsycopg:
            Error = FakeError

            @staticmethod
            def connect(url, *, autocommit, row_factory):
                self.assertEqual(url, "postgresql://fixture.invalid/topik")
                self.assertFalse(autocommit)
                self.assertIsNotNone(row_factory)
                return raw

        with patch.object(database, "_psycopg_tuple", return_value=(FakePsycopg, object())):
            connection = database.PostgresAuditConnection(
                "postgresql://fixture.invalid/topik", readonly=True
            )
            result = connection.execute("SELECT value FROM sample WHERE id=? AND note='really?'", (7,))
            self.assertEqual(result.fetchone(), ("ok",))
            connection.rollback()
            connection.close()

        self.assertEqual(raw.statements[0][0], "SET default_transaction_isolation = 'repeatable read'")
        self.assertEqual(raw.statements[1][0], "SET default_transaction_read_only = on")
        self.assertEqual(
            raw.statements[2],
            ("SELECT value FROM sample WHERE id=%s AND note='really?'", (7,)),
        )
        self.assertEqual(raw.commits, 1)
        self.assertEqual(raw.rollbacks, 1)
        self.assertTrue(raw.closed)

    def test_reviewer_dict_row_connection_is_not_accepted_as_audit_connection(self):
        class ReviewerConnection:
            backend = "postgres"

        with self.assertRaisesRegex(ai_audit_35.AuditError, "tuple-row PostgreSQL audit adapter"):
            with ai_audit_35._connection(ReviewerConnection()):
                pass

    def test_repeatable_write_transaction_rolls_back_once_without_retry(self):
        class FakeAuditDB:
            backend = "postgres"
            ai_audit_tuple_rows = True

            def __init__(self):
                self.statements = []
                self.commits = 0
                self.rollbacks = 0

            def execute(self, sql, params=()):
                self.statements.append((sql, params))
                return Cursor()

            def commit(self):
                self.commits += 1

            def rollback(self):
                self.rollbacks += 1

        db = FakeAuditDB()
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with ai_audit_35._write_transaction(db, repeatable_read=True):
                raise RuntimeError("fixture failure")
        self.assertEqual(db.statements, [("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", ())])
        self.assertEqual(db.commits, 0)
        self.assertEqual(db.rollbacks, 1)

    def test_attempt_number_is_allocated_only_after_pass_lock_and_uses_returning(self):
        class FakeAuditDB:
            backend = "postgres"
            ai_audit_tuple_rows = True

            def __init__(self):
                self.statements = []

            def execute(self, sql, params=()):
                self.statements.append((sql, params))
                if "FOR UPDATE" in sql:
                    return Cursor([("pass-1",)])
                if "MAX(attempt_number)" in sql:
                    return Cursor([(5,)])
                if "clock_timestamp" in sql:
                    return Cursor([("2026-10-06T09:00:00.000001+00:00",)])
                if "INSERT INTO ai_audit_attempts" in sql:
                    return Cursor([(77,)])
                raise AssertionError(sql)

        db = FakeAuditDB()
        attempt = ai_audit_35._insert_attempt(
            db,
            "pass-1",
            "failed",
            error_code="provider_error",
            error_message="fixture",
            evidence={"http_status": 503},
        )
        statements = [sql for sql, _ in db.statements]
        lock_index = next(i for i, sql in enumerate(statements) if "FOR UPDATE" in sql)
        max_index = next(i for i, sql in enumerate(statements) if "MAX(attempt_number)" in sql)
        insert_index = next(i for i, sql in enumerate(statements) if "INSERT INTO ai_audit_attempts" in sql)
        self.assertLess(lock_index, max_index)
        self.assertLess(max_index, insert_index)
        self.assertIn("RETURNING id", statements[insert_index])
        self.assertEqual(attempt["id"], 77)
        self.assertEqual(attempt["attempt_number"], 5)

    def test_postgres_table_discovery_never_uses_sqlite_master(self):
        class FakeAuditDB:
            backend = "postgres"
            ai_audit_tuple_rows = True

            def execute(self, sql, params=()):
                if "sqlite_master" in sql or "information_schema.tables" not in sql:
                    raise AssertionError(sql)
                return Cursor([("ai_audit_runs",), ("questions",)])

        self.assertEqual(
            ai_audit_35._table_names(FakeAuditDB()),
            {"ai_audit_runs", "questions"},
        )

    def test_implicit_target_prefers_postgres_env_but_explicit_sqlite_wins(self):
        url = "postgresql://fixture.invalid/topik"
        explicit = Path("fixture.sqlite")
        with patch.dict(os.environ, {"TOPIK_DATABASE_URL": url}):
            self.assertEqual(ai_audit_35._resolve_target(None), url)
            self.assertEqual(ai_audit_35._resolve_target(explicit), explicit)

    def test_postgres_timestamp_uses_server_clock_and_order_never_uses_rowid(self):
        class FakeAuditDB:
            backend = "postgres"
            ai_audit_tuple_rows = True

            def __init__(self):
                self.statements = []

            def execute(self, sql, params=()):
                self.statements.append((sql, params))
                return Cursor([("2026-10-06T09:12:34.123456+00:00",)])

        db = FakeAuditDB()
        self.assertEqual(
            ai_audit_35._created_at(db),
            "2026-10-06T09:12:34.123456+00:00",
        )
        self.assertIn("clock_timestamp", db.statements[0][0])
        order = ai_audit_35._run_order(db)
        self.assertEqual(order, "r.created_at DESC,r.id DESC")
        self.assertNotIn("rowid", order)

    def test_checkpoint_race_contract_locks_pass_before_sequence_allocation(self):
        class FakeAuditDB:
            backend = "postgres"
            ai_audit_tuple_rows = True

            def __init__(self):
                self.statements = []
                self.commits = 0
                self.rollbacks = 0

            def execute(self, sql, params=()):
                self.statements.append((sql, params))
                if "FOR UPDATE" in sql:
                    return Cursor([("pass-1",)])
                if "SELECT 1 FROM ai_audit_results" in sql:
                    return Cursor()
                if "SELECT sequence,completed_subject_ids_json" in sql:
                    return Cursor([(4, '["q1"]')])
                if "clock_timestamp" in sql:
                    return Cursor([("2026-10-06T09:20:00.000001+00:00",)])
                if "INSERT INTO ai_audit_checkpoints" in sql:
                    return Cursor()
                raise AssertionError(sql)

            def commit(self):
                self.commits += 1

            def rollback(self):
                self.rollbacks += 1

        db = FakeAuditDB()
        normalized = {
            "completed_subject_ids": ["q1"],
            "verdicts": [],
            "findings": [],
            "notes": [],
        }
        with (
            patch.object(ai_audit_35, "ensure_schema"),
            patch.object(ai_audit_35, "_load_pass", return_value=(None, {})),
            patch.object(ai_audit_35, "_normalize_result_payload", return_value=normalized),
        ):
            saved = ai_audit_35.save_checkpoint(db, "pass-1", {}, state={"cursor": 4})

        statements = [sql for sql, _ in db.statements]
        lock_index = next(i for i, sql in enumerate(statements) if "FOR UPDATE" in sql)
        result_index = next(i for i, sql in enumerate(statements) if "SELECT 1 FROM ai_audit_results" in sql)
        sequence_index = next(i for i, sql in enumerate(statements) if "SELECT sequence" in sql)
        insert_index = next(i for i, sql in enumerate(statements) if "INSERT INTO ai_audit_checkpoints" in sql)
        self.assertLess(lock_index, result_index)
        self.assertLess(result_index, sequence_index)
        self.assertLess(sequence_index, insert_index)
        self.assertEqual(saved["sequence"], 5)
        self.assertEqual((db.commits, db.rollbacks), (1, 0))

    def test_pass_slot_race_contract_locks_run_before_creating_pass(self):
        class FakeAuditDB:
            backend = "postgres"
            ai_audit_tuple_rows = True

            def __init__(self):
                self.statements = []
                self.commits = 0

            def execute(self, sql, params=()):
                self.statements.append((sql, params))
                if "FROM ai_audit_runs" in sql and "FOR UPDATE" in sql:
                    return Cursor([("run-1",)])
                if "SELECT snapshot_sha256,contract_version" in sql:
                    return Cursor([("snap-1", ai_audit_35.CONTRACT_VERSION)])
                if "SELECT snapshot_json" in sql:
                    return Cursor([('{"questions":[]}',)])
                raise AssertionError(sql)

            def commit(self):
                self.commits += 1

            def rollback(self):
                raise AssertionError("unexpected rollback")

        db = FakeAuditDB()
        insert_seen_after_lock = []

        def fake_insert_pass(*_args, **_kwargs):
            insert_seen_after_lock.append(any("FOR UPDATE" in sql for sql, _ in db.statements))
            return {"id": "pass-1"}

        with (
            patch.object(ai_audit_35, "ensure_schema"),
            patch.object(ai_audit_35, "_insert_pass", side_effect=fake_insert_pass),
            patch.object(ai_audit_35, "_load_pass", return_value=(None, {"pass_id": "pass-1"})),
        ):
            exported = ai_audit_35.export_pass(
                db,
                run_id="run-1",
                pass_number=1,
                auditor_id="auditor-a",
                perspective="independent",
                model_id="model-a",
            )

        self.assertEqual(exported["pass_id"], "pass-1")
        self.assertEqual(insert_seen_after_lock, [True])
        self.assertEqual(db.commits, 1)

    def test_final_result_race_contract_relocks_before_write_and_uses_returning(self):
        class FakeAuditDB:
            backend = "postgres"
            ai_audit_tuple_rows = True

            def __init__(self):
                self.statements = []
                self.commits = 0
                self.rollbacks = 0

            def execute(self, sql, params=()):
                self.statements.append((sql, params))
                if "FOR UPDATE" in sql:
                    return Cursor([("pass-1",)])
                if "SELECT id,result_sha256,raw_json FROM ai_audit_results" in sql:
                    return Cursor()
                if "clock_timestamp" in sql:
                    return Cursor([("2026-10-06T09:21:00.000001+00:00",)])
                if "INSERT INTO ai_audit_results" in sql:
                    if "RETURNING id" not in sql:
                        raise AssertionError("PostgreSQL result insert must use RETURNING id")
                    return Cursor([(91,)])
                if "MAX(attempt_number)" in sql:
                    return Cursor([(1,)])
                if "INSERT INTO ai_audit_attempts" in sql:
                    if "RETURNING id" not in sql:
                        raise AssertionError("PostgreSQL attempt insert must use RETURNING id")
                    return Cursor([(92,)])
                raise AssertionError(sql)

            def commit(self):
                self.commits += 1

            def rollback(self):
                self.rollbacks += 1

        db = FakeAuditDB()
        bundle = {"result_contract": {"findings": {"fingerprint_version": ai_audit_35.FINDING_FINGERPRINT_VERSION}}}
        normalized = {
            "completed_subject_ids": ["q1"],
            "verdicts": [{"subject_id": "q1", "verdict": "clear", "confidence": 1.0, "rationale": "ok"}],
            "findings": [],
            "notes": [],
        }
        with (
            patch.object(ai_audit_35, "ensure_schema"),
            patch.object(ai_audit_35, "_load_pass", return_value=(None, bundle)),
            patch.object(ai_audit_35, "_normalize_result_payload", return_value=normalized),
        ):
            imported = ai_audit_35.import_result(db, {"pass_id": "pass-1"})

        statements = [sql for sql, _ in db.statements]
        outer_lock = next(i for i, sql in enumerate(statements) if "FOR UPDATE" in sql)
        existing_result = next(i for i, sql in enumerate(statements) if "SELECT id,result_sha256,raw_json" in sql)
        result_insert = next(i for i, sql in enumerate(statements) if "INSERT INTO ai_audit_results" in sql)
        attempt_max = next(i for i, sql in enumerate(statements) if "MAX(attempt_number)" in sql)
        self.assertLess(outer_lock, existing_result)
        self.assertLess(existing_result, result_insert)
        self.assertLess(result_insert, attempt_max)
        self.assertEqual(imported["result_id"], 91)
        self.assertEqual(imported["attempt"]["attempt_number"], 1)
        self.assertEqual((db.commits, db.rollbacks), (1, 0))


if __name__ == "__main__":
    unittest.main()
