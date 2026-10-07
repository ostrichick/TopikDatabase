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

    def test_ai_audit_summary_is_cached_in_memory_per_question(self):
        qid = self.listening_id
        fake_module = MagicMock(
            summarize_question=MagicMock(return_value={"run_id": "run-1", "verdict": "clear", "total": 1}),
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

            # Second call must hit memory cache and not query backend again
            second = self.store.get_question(qid)
            self.assertEqual(fake_module.summarize_question.call_count, 1)
            self.assertEqual(first.get("ai_audit"), second.get("ai_audit"))

            # Cache invalidation forces refresh
            self.store.clear_ai_audit_cache()
            third = self.store.get_question(qid)
            self.assertEqual(fake_module.summarize_question.call_count, 2)
            self.assertEqual(first.get("ai_audit"), third.get("ai_audit"))

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
