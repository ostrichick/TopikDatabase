"""Equivalence and query-budget regressions for the bulk 35th audit list."""

from contextlib import closing
import sqlite3
import unittest

from src import ai_audit_35, ai_audit_list_summary
from src.review_ui import ReviewStore
from tests import test_ai_audit_35 as fixture_module


class CountingSQLite:
    ai_audit_tuple_rows = True

    def __init__(self, connection):
        self.connection = connection
        self.selects = 0

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("SELECT"):
            self.selects += 1
        return self.connection.execute(sql, params)

    def rollback(self):
        self.connection.rollback()


class TuplePostgresEmulation:
    """Exercise the tuple-row PG adapter contract against synthetic SQLite data."""

    backend = "postgres"
    ai_audit_tuple_rows = True

    def __init__(self, connection):
        self.connection = connection
        self.selects = 0
        self.rollbacks = 0

    def execute(self, sql, params=()):
        if sql.startswith("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"):
            self.connection.execute("BEGIN")
            return None
        if "information_schema.tables" in sql:
            self.selects += 1
            return self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        # PG has no rowid tie-breaker, but this single-run fixture needs no tie.
        if sql.lstrip().upper().startswith("SELECT"):
            self.selects += 1
        return self.connection.execute(sql, params)

    def rollback(self):
        self.rollbacks += 1
        self.connection.rollback()


class BulkAuditListTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.AIAudit35Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.path = self.fixture.db_path

    def assert_parity(self):
        baseline = {item["id"]: item["ai_audit"] for item in
                    ReviewStore(db_path=self.path).list_questions()["items"] if "ai_audit" in item}
        fast = ai_audit_list_summary.summarize_list_bulk(self.path)
        self.assertEqual(fast, baseline)
        for qid, item in fast.items():
            summary = ai_audit_35.summarize_question(self.path, qid)
            run_id = summary.get("run_id") if summary else None
            status = ai_audit_35.status_report(self.path, run_id, subject_id=qid)
            self.assertEqual(item["attempt_total"], status["latest_run"]["attempt_total"])
            self.assertEqual(item["latest_run"]["subject_id"], qid)
            if summary:
                self.assertEqual(item["risk_score"], summary["risk_score"])
                self.assertEqual(item["risk_level"], summary["risk_level"])
                self.assertEqual(item["convergence"], summary["convergence"])
                self.assertEqual(item["cross_run_convergence"], summary["cross_run_convergence"])
                self.assertEqual(item["unresolved_findings"], len(summary["unresolved_findings"]))

    def test_no_audit_tables(self):
        self.assertEqual(ai_audit_list_summary.summarize_list_bulk(self.path), {})

    def test_pending_scoped_passes_and_failure_only_are_subject_specific(self):
        before = self.fixture._human_state()
        run = ai_audit_35.create_run(self.path, label="scope")
        listen = ai_audit_35.export_pass(
            self.path, run_id=run["run_id"], pass_number=1,
            auditor_id="listen", perspective="transcript_alignment", model_id="test", prompt_version="v1")
        ai_audit_35.export_pass(
            self.path, run_id=run["run_id"], pass_number=2,
            auditor_id="all", perspective="independent", model_id="test", prompt_version="v1")
        ai_audit_35.record_attempt_outcome(self.path, listen["pass_id"], "timed_out",
                                           error_code="timeout", error_message="retry")
        self.assert_parity()
        summary = ai_audit_list_summary.summarize_list_bulk(self.path)
        self.assertEqual(summary[fixture_module.Q1]["attempt_total"], 1)
        self.assertEqual(summary[fixture_module.Q2]["attempt_total"], 0)
        self.assertTrue(summary[fixture_module.Q1]["has_partial_failures"])
        self.assertFalse(summary[fixture_module.Q2]["has_partial_failures"])
        self.assertEqual(self.fixture._human_state(), before)

    def test_findings_recurrence_multiple_runs_latest_selection_and_failed_retry(self):
        run1 = ai_audit_35.create_run(self.path, subject_ids=[fixture_module.Q1, fixture_module.Q2],
                                      label="initial")
        first = ai_audit_35.export_pass(self.path, run_id=run1["run_id"], pass_number=1,
                                        auditor_id="first", perspective="independent",
                                        model_id="test", prompt_version="v1")
        second = ai_audit_35.export_pass(self.path, run_id=run1["run_id"], pass_number=2,
                                         auditor_id="second", perspective="independent",
                                         model_id="test", prompt_version="v1")
        ai_audit_35.ingest_result(self.path, self.fixture._result(
            {"id": first["pass_id"]}, finding=True, finding_subject=fixture_module.Q2))
        ai_audit_35.ingest_result(self.path, self.fixture._result(
            {"id": second["pass_id"]}, finding=True, finding_subject=fixture_module.Q2))
        self.assert_parity()

        run2 = ai_audit_35.create_run(self.path, subject_ids=[fixture_module.Q2], label="targeted")
        res = ai_audit_35.export_pass(self.path, run_id=run2["run_id"], pass_number=1,
                                      auditor_id="new", perspective="independent", model_id="test", prompt_version="v2")
        pending = ai_audit_35.export_pass(self.path, run_id=run2["run_id"], pass_number=2,
                                          auditor_id="new2", perspective="independent", model_id="test", prompt_version="v2")
        ai_audit_35.ingest_result(self.path, self.fixture._result(
            {"id": res["pass_id"]}, completed=[fixture_module.Q2]))
        ai_audit_35.record_attempt_outcome(self.path, pending["pass_id"], "failed",
                                           error_code="failure", error_message="retry")
        ai_audit_35.record_attempt_outcome(self.path, pending["pass_id"], "timed_out",
                                           error_code="timeout", error_message="retry 2")
        self.assert_parity()

        run3 = ai_audit_35.create_run(self.path, subject_ids=[fixture_module.Q1], label="new pending")
        ai_audit_35.export_pass(self.path, run_id=run3["run_id"], pass_number=1,
                                auditor_id="other", perspective="transcript_alignment",
                                model_id="test", prompt_version="v3")
        self.assert_parity()
        results = ai_audit_list_summary.summarize_list_bulk(self.path)
        self.assertEqual(results[fixture_module.Q1]["run_id"], run1["run_id"])
        self.assertEqual(results[fixture_module.Q2]["run_id"], run2["run_id"])
        self.assertEqual(results[fixture_module.Q2]["retry_count"], 1)

    def test_tuple_postgres_read_only_adapter_and_snapshot(self):
        run = self.fixture._run()
        ai_audit_35.ingest_result(self.path, self.fixture._result(run["passes"][0]))
        sqlite_expected = ai_audit_list_summary.summarize_list_bulk(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            pg = TuplePostgresEmulation(db)
            self.assertEqual(ai_audit_list_summary.summarize_list_bulk(pg), sqlite_expected)
            self.assertLessEqual(pg.selects, 20)
            self.assertEqual(pg.rollbacks, 1)

    def test_seventy_questions_are_processed_with_bounded_sql(self):
        all_subjects = [fixture_module.Q1, fixture_module.Q2]
        with closing(sqlite3.connect(self.path)) as db:
            # Clone only local test questions and their answers/choices; do not
            # materialize or alter any actual 35th corpus/database.
            for number in range(2, 71):
                if number == 31:
                    continue
                listening = number <= 30
                template = fixture_module.Q1 if listening else fixture_module.Q2
                qid = f"035-I-{'L' if listening else 'R'}-{number:03d}"
                all_subjects.append(qid)
                db.execute(
                    "INSERT INTO questions (id,section_id,group_id,source_file_id,exam_number,"
                    "answer_key_number,source_pdf_page,printed_page,points,stem,raw_question_text,"
                    "passage_id,requires_image,review_status,extraction_origin,preview_flags_json) "
                    "SELECT ?,section_id,group_id,source_file_id,?,?,source_pdf_page,"
                    "printed_page,points,stem,raw_question_text,passage_id,requires_image,review_status,"
                    "extraction_origin,preview_flags_json FROM questions WHERE id=?",
                    (qid, number, number if listening else number-30, template))
                db.execute("INSERT INTO choices(question_id,number,text) SELECT ?,number,text "
                           "FROM choices WHERE question_id=?", (qid, template))
                db.execute("INSERT INTO answers SELECT ?,choice_number,source_file_id,source_pdf_page,"
                           "preview_and_pdf_agree FROM answers WHERE question_id=?", (qid, template))
            db.commit()
        run = ai_audit_35.create_run(self.path, label="70 subjects")
        exported = ai_audit_35.export_pass(self.path, run_id=run["run_id"], pass_number=1,
                                           auditor_id="all-70", perspective="independent",
                                           model_id="test", prompt_version="v1")
        ai_audit_35.ingest_result(self.path, self.fixture._result(
            {"id": exported["pass_id"]}, completed=all_subjects,
            finding=True, finding_subject=fixture_module.Q1))
        with closing(sqlite3.connect(self.path)) as db:
            counter = CountingSQLite(db)
            rows = ai_audit_list_summary.summarize_list_bulk(counter)
            self.assertEqual(len(rows), 70)
            self.assertLessEqual(counter.selects, 20, f"SQL count grows with subjects: {counter.selects}")
            self.assertTrue(all(row["latest_run"]["subject_id"] == qid for qid, row in rows.items()))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_audit_attempts").fetchone()[0], 1)
        # Full 70-subject golden parity uses the legacy SQLite path as an
        # independent oracle; only the bulk call above is SQL-budgeted.
        old_list = ReviewStore(db_path=self.path).list_questions()
        old_audit = {item["id"]: item["ai_audit"] for item in old_list["items"] if "ai_audit" in item}
        self.assertEqual(rows, old_audit)


if __name__ == "__main__":
    unittest.main()
