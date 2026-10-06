"""Offline tests for the append-only independent AI audit foundation."""

import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path

from src import ai_audit_35


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "db" / "schema.sql"
Q1 = "035-I-L-001"
Q2 = "035-I-R-031"


class AIAudit35Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="topik-ai-audit-")
        self.db_path = Path(self.temp.name) / "audit.sqlite"
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA.read_text(encoding="utf-8"))
            db.execute("INSERT INTO exams VALUES(?,?,?,?)", ("035-I-B", 35, "I", "B"))
            sources = [
                (1, "paper.pdf", "test_paper", "1" * 64, 100),
                (2, "answer.pdf", "answer_key", "2" * 64, 100),
                (3, "transcript.pdf", "listening_transcript", "3" * 64, 100),
                (4, "audio.mp3", "listening_audio", "4" * 64, 100),
            ]
            db.executemany(
                "INSERT INTO source_files(id,relative_path,kind,sha256,byte_size) VALUES(?,?,?,?,?)",
                sources,
            )
            db.executemany(
                "INSERT INTO sections VALUES(?,?,?,?,?,?)",
                [
                    ("035-I-B-listening", "035-I-B", "listening", 1, 30, 0),
                    ("035-I-B-reading", "035-I-B", "reading", 31, 70, 30),
                ],
            )
            db.executemany(
                "INSERT INTO question_groups VALUES(?,?,?,?,?,?,?,?)",
                [
                    ("g1", "035-I-B-listening", 1, 1, "듣고 고르십시오.", "", 4, None),
                    ("g2", "035-I-B-reading", 31, 31, "읽고 고르십시오.", "", 2, None),
                ],
            )
            db.executemany(
                "INSERT INTO questions(id,section_id,group_id,source_file_id,exam_number,answer_key_number,"
                "source_pdf_page,printed_page,points,stem,raw_question_text,passage_id,requires_image,"
                "review_status,extraction_origin,preview_flags_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (Q1, "035-I-B-listening", "g1", 1, 1, 1, 2, 1, 4, "무엇을 삽니까?", "무엇을 삽니까?", None, 0, "verified", "fixture", "[]"),
                    (Q2, "035-I-B-reading", "g2", 1, 31, 1, 10, 9, 2, "알맞은 것을 고르십시오.", "알맞은 것을 고르십시오.", None, 0, "needs_manual_review", "fixture", "[]"),
                ],
            )
            db.executemany(
                "INSERT INTO choices(question_id,number,text) VALUES(?,?,?)",
                [(qid, number, f"선택 {number}") for qid in (Q1, Q2) for number in range(1, 5)],
            )
            db.executemany(
                "INSERT INTO answers VALUES(?,?,?,?,?)",
                [(Q1, 1, 2, 1, 1), (Q2, 2, 2, 2, 1)],
            )
            db.execute(
                "INSERT INTO transcripts VALUES(?,?,?,?,?,?)",
                (Q1, 3, 1, "여자: 사과 주세요.", "verified", "[]"),
            )
            db.execute(
                "INSERT INTO audio_assets VALUES(?,?,?,?,?)",
                ("035-I-B-audio", "035-I-B-listening", 4, 120.0, "not_segmented"),
            )
            db.execute(
                "INSERT INTO audio_segments(question_id,audio_asset_id,start_ms,end_ms,status,version,source_sha256,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (Q1, "035-I-B-audio", 1000, 2000, "verified", 1, "4" * 64, "2026-10-05T00:00:00Z"),
            )
            db.execute(
                "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("question", Q1, "verified", "human", "fixture", "manual check", "2026-10-05T00:00:00Z"),
            )
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def _human_state(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            return {
                "questions": db.execute("SELECT id,review_status FROM questions ORDER BY id").fetchall(),
                "transcripts": db.execute("SELECT question_id,review_status FROM transcripts ORDER BY question_id").fetchall(),
                "segments": db.execute("SELECT question_id,status,version FROM audio_segments ORDER BY question_id").fetchall(),
                "reviews": db.execute("SELECT * FROM review_records ORDER BY id").fetchall(),
            }

    def _run(self, count=1):
        return ai_audit_35.create_run(
            self.db_path,
            auditors=[f"agent-{index}" for index in range(1, count + 1)],
            model_id="gpt-test",
            prompt_version="audit-prompt-v7",
            perspective="content-and-answer",
            label="fixture",
        )

    def _result(self, pass_info, *, kind="final", completed=None, finding=False, finding_subject=Q1,
                severity="medium", summary="답 불일치", state=None):
        exported = ai_audit_35.export_pass(self.db_path, pass_id=pass_info["id"])
        completed = completed or [Q1, Q2]
        verdicts = []
        for qid in completed:
            verdicts.append(
                {
                    "subject_id": qid,
                    "verdict": "finding" if finding and qid == finding_subject else "clear",
                    "confidence": 0.9,
                    "rationale": "독립 검수",
                }
            )
        findings = []
        if finding and finding_subject in completed:
            findings.append(
                {
                    "subject_id": finding_subject,
                    "category": "answer",
                    "severity": severity,
                    "summary": summary,
                    "detail": "근거 필드 비교",
                    "evidence": {"field": "answer.choice_number", "observed": 1, "expected": 2},
                }
            )
        return {
            "schema_version": ai_audit_35.RESULT_SCHEMA_VERSION,
            "kind": kind,
            "pass_id": pass_info["id"],
            "input_sha256": exported["input_sha256"],
            "snapshot_sha256": exported["snapshot_sha256"],
            "completed_subject_ids": completed,
            "verdicts": verdicts,
            "findings": findings,
            "notes": ["fixture"],
            "state": state or {},
        }

    def _custom_result(self, pass_id, verdicts, findings=None, notes=None):
        exported = ai_audit_35.export_pass(self.db_path, pass_id=pass_id)
        return {
            "schema_version": ai_audit_35.RESULT_SCHEMA_VERSION,
            "kind": "final",
            "pass_id": pass_id,
            "input_sha256": exported["input_sha256"],
            "snapshot_sha256": exported["snapshot_sha256"],
            "completed_subject_ids": [item["subject_id"] for item in verdicts],
            "verdicts": verdicts,
            "findings": findings or [],
            "notes": notes or ["synthetic aggregation fixture"],
            "state": {},
        }

    def test_blind_export_persists_provenance_and_never_changes_human_state(self):
        before = self._human_state()
        run = self._run(2)
        self.assertEqual(self._human_state(), before)
        first = run["passes"][0]
        exported = ai_audit_35.export_pass(self.db_path, pass_id=first["id"])
        serialized = json.dumps(exported, ensure_ascii=False)
        self.assertTrue(exported["blind"])
        self.assertEqual(exported["model_id"], "gpt-test")
        self.assertEqual(exported["prompt_version"], "audit-prompt-v7")
        self.assertIn("answer_key_number", exported["instructions"]["source_mapping"])
        self.assertIn("exam_number", exported["instructions"]["source_mapping"])
        self.assertIn("internally conflicting", exported["instructions"]["calibration"])
        self.assertEqual(
            exported["result_contract"]["verdicts"]["required"],
            ["subject_id", "verdict", "confidence", "rationale"],
        )
        self.assertIn("identity", exported["result_contract"]["findings"]["required"])
        self.assertEqual(
            exported["result_contract"]["findings"]["fingerprint_version"],
            ai_audit_35.FINDING_FINGERPRINT_VERSION,
        )
        self.assertIn("stable structured defect key", exported["result_contract"]["findings"]["identity"])
        self.assertIn("object or array", exported["result_contract"]["findings"]["evidence"])
        self.assertIn("array of short strings", exported["result_contract"]["notes"])
        self.assertNotIn("review_status", serialized)
        self.assertNotIn("review_records", serialized)
        self.assertNotIn("audio_segments", serialized)
        with closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute(
                "SELECT auditor_id,model_id,prompt_version,perspective,blind,input_sha256 "
                "FROM ai_audit_passes WHERE id=?",
                (first["id"],),
            ).fetchone()
        self.assertEqual(row[:5], ("agent-1", "gpt-test", "audit-prompt-v7", "content-and-answer", 1))
        self.assertEqual(row[5], exported["input_sha256"])

    def test_deterministic_finding_dedup_consensus_risk_and_read_only_summaries(self):
        run = self._run(3)
        passes = run["passes"]
        ai_audit_35.ingest_result(self.db_path, self._result(passes[0], finding=True, severity="medium", summary="첫 표현"))
        ai_audit_35.ingest_result(self.db_path, self._result(passes[1], finding=True, severity="high", summary="다른 문장"))
        ai_audit_35.ingest_result(self.db_path, self._result(passes[2], finding=False))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_audit_findings").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_audit_finding_occurrences").fetchone()[0], 2)
        consensus = ai_audit_35.consensus_report(self.db_path, run["run_id"])
        self.assertEqual(consensus["completed_passes"], 3)
        self.assertEqual(consensus["findings"][0]["consensus_count"], 2)
        self.assertAlmostEqual(consensus["findings"][0]["consensus_ratio"], 0.6667, places=4)
        self.assertEqual(consensus["findings"][0]["max_severity"], "high")
        self.assertTrue(consensus["convergence"]["converged"])
        self.assertEqual(consensus["convergence"]["new_findings_latest_pass"], 0)

        q1 = ai_audit_35.summarize_question(self.db_path, Q1, run["run_id"])
        self.assertEqual(q1["verdict"], "finding")
        self.assertTrue(q1["disagreement"])
        self.assertEqual(q1["entries"][0]["model_id"], "gpt-test")
        self.assertEqual(q1["entries"][0]["prompt_version"], "audit-prompt-v7")
        self.assertEqual(q1["entries"][0]["findings"][0]["evidence"]["observed"], 1)
        self.assertEqual(q1["findings"][0]["evidence"][0]["expected"], 2)
        overall = ai_audit_35.summarize_all_questions(self.db_path, run["run_id"])
        self.assertEqual(overall["totals"], {"clear": 1, "finding": 1, "uncertain": 0})
        self.assertEqual(overall["entries"], 3)

    def test_mixed_scope_consensus_uses_subject_eligible_denominator_and_raw_occurrences(self):
        run = ai_audit_35.create_run(self.db_path, subject_ids=[Q1, Q2], label="mixed-scope-math")
        p1 = ai_audit_35.export_pass(
            self.db_path, run_id=run["run_id"], pass_number=1, auditor_id="all-a",
            perspective="independent", model_id="gpt-test", prompt_version="math-v1",
        )
        p2 = ai_audit_35.export_pass(
            self.db_path, run_id=run["run_id"], pass_number=2, auditor_id="listen-only",
            perspective="transcript_alignment", model_id="gpt-test", prompt_version="math-v1",
        )
        p3 = ai_audit_35.export_pass(
            self.db_path, run_id=run["run_id"], pass_number=3, auditor_id="all-b",
            perspective="independent", model_id="gpt-test", prompt_version="math-v1",
        )
        shared_identity = {"field": "stem", "issue": "synthetic-shared"}
        second_identity = {"field": "answer", "issue": "synthetic-second"}
        finding_a = {
            "subject_id": Q2, "category": "text_fidelity", "severity": "medium",
            "summary": "shared finding A", "detail": "first observation",
            "identity": shared_identity,
            "evidence": {"field": "stem", "observed": "alpha", "expected": "beta"},
        }
        finding_b = {
            "subject_id": Q2, "category": "text_fidelity", "severity": "high",
            "summary": "shared finding B", "detail": "conflicting observation, same identity",
            "identity": shared_identity,
            "evidence": {"field": "stem", "observed": "gamma", "expected": "beta"},
        }
        finding_second = {
            "subject_id": Q2, "category": "answer", "severity": "low",
            "summary": "second fingerprint", "detail": "only one eligible pass reports this",
            "identity": second_identity,
            "evidence": {"field": "answer.choice_number", "observed": 1, "expected": 2},
        }
        ai_audit_35.ingest_response(self.db_path, p1["pass_id"], self._custom_result(
            p1["pass_id"],
            [
                {"subject_id": Q1, "verdict": "clear", "confidence": 0.9, "rationale": "normal"},
                {"subject_id": Q2, "verdict": "finding", "confidence": 0.9, "rationale": "finding A"},
            ],
            [finding_a],
        ))
        ai_audit_35.ingest_response(self.db_path, p2["pass_id"], self._custom_result(
            p2["pass_id"],
            [{"subject_id": Q1, "verdict": "uncertain", "confidence": 0.5, "rationale": "uncertain evidence"}],
        ))
        ai_audit_35.ingest_response(self.db_path, p3["pass_id"], self._custom_result(
            p3["pass_id"],
            [
                {"subject_id": Q1, "verdict": "clear", "confidence": 0.95, "rationale": "normal again"},
                {"subject_id": Q2, "verdict": "finding", "confidence": 0.95, "rationale": "two findings"},
            ],
            [finding_b, finding_second],
        ))

        with closing(sqlite3.connect(self.db_path)) as db:
            raw = {
                "results": db.execute(
                    "SELECT COUNT(*) FROM ai_audit_results r JOIN ai_audit_passes p ON p.id=r.pass_id WHERE p.run_id=?",
                    (run["run_id"],),
                ).fetchone()[0],
                "findings": db.execute("SELECT COUNT(*) FROM ai_audit_findings").fetchone()[0],
                "occurrences": db.execute(
                    "SELECT COUNT(*) FROM ai_audit_finding_occurrences o JOIN ai_audit_passes p ON p.id=o.pass_id WHERE p.run_id=?",
                    (run["run_id"],),
                ).fetchone()[0],
            }
            per_fingerprint = dict(db.execute(
                "SELECT o.fingerprint,COUNT(*) FROM ai_audit_finding_occurrences o "
                "JOIN ai_audit_passes p ON p.id=o.pass_id WHERE p.run_id=? GROUP BY o.fingerprint",
                (run["run_id"],),
            ).fetchall())
        self.assertEqual(raw, {"results": 3, "findings": 2, "occurrences": 3})
        self.assertEqual(sorted(per_fingerprint.values()), [1, 2])

        consensus = ai_audit_35.consensus_report(self.db_path, run["run_id"])
        self.assertEqual(consensus["completed_passes"], 3)
        by_count = {item["consensus_count"]: item for item in consensus["findings"]}
        repeated = by_count[2]
        single = by_count[1]
        self.assertEqual(repeated["eligible_completed_passes"], 2)
        self.assertEqual(repeated["consensus_ratio"], 1.0)
        self.assertEqual(single["eligible_completed_passes"], 2)
        self.assertEqual(single["consensus_ratio"], 0.5)

        q1 = ai_audit_35.summarize_question(self.db_path, Q1, run["run_id"])
        self.assertEqual(q1["totals"], {"clear": 2, "finding": 0, "uncertain": 1})
        self.assertTrue(q1["disagreement"])
        self.assertEqual(q1["latest_run"]["pass_total"], 3)
        self.assertEqual(q1["latest_run"]["completed_passes"], 3)
        q2 = ai_audit_35.summarize_question(self.db_path, Q2, run["run_id"])
        self.assertEqual(q2["totals"], {"clear": 0, "finding": 2, "uncertain": 0})
        self.assertFalse(q2["disagreement"])
        self.assertEqual(q2["latest_run"]["pass_total"], 2)
        self.assertEqual(q2["latest_run"]["completed_passes"], 2)
        self.assertEqual(sorted(item["occurrences"] for item in q2["findings"]), [1, 2])
        repeated_summary = next(item for item in q2["findings"] if item["occurrences"] == 2)
        self.assertEqual(len(repeated_summary["evidence"]), 2)
        self.assertNotEqual(repeated_summary["evidence"][0], repeated_summary["evidence"][1])

    def test_new_fingerprint_contract_dedups_same_identity_across_category_wording(self):
        run = self._run(2)
        first, second = run["passes"]
        identity = {"field": "answer.choice_number", "observed": 2, "expected": 4}
        result_a = self._result(first, finding=True)
        result_b = self._result(second, finding=True)
        for result, category in ((result_a, "answer_choice_mismatch"), (result_b, "answer_key_mismatch")):
            result["findings"][0]["category"] = category
            result["findings"][0]["identity"] = identity
            result["findings"][0]["evidence"] = dict(identity)
        ai_audit_35.ingest_result(self.db_path, result_a)
        ai_audit_35.ingest_result(self.db_path, result_b)
        consensus = ai_audit_35.consensus_report(self.db_path, run["run_id"])
        self.assertEqual(len(consensus["findings"]), 1)
        self.assertEqual(consensus["findings"][0]["consensus_count"], 2)
        self.assertEqual(consensus["findings"][0]["eligible_completed_passes"], 2)
        self.assertEqual(consensus["findings"][0]["consensus_ratio"], 1.0)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_audit_findings").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_audit_finding_occurrences").fetchone()[0], 2)

        duplicate = self._result(self._run(1)["passes"][0], finding=True)
        duplicate["findings"][0]["identity"] = identity
        duplicate["findings"][0]["evidence"] = dict(identity)
        second_copy = dict(duplicate["findings"][0], category="answer_key_mismatch")
        duplicate["findings"].append(second_copy)
        with self.assertRaises(ai_audit_35.AuditError):
            ai_audit_35.ingest_result(self.db_path, duplicate)

    def test_prior_results_never_enter_new_blind_pass_inputs_or_cross_database_state(self):
        isolated = Path(self.temp.name) / "second.sqlite"
        shutil.copy2(self.db_path, isolated)
        first = self._run(1)
        sentinel = "PRIOR-AUDIT-SECRET-SENTINEL"
        result = self._result(first["passes"][0], finding=True, summary=sentinel)
        result["notes"] = [sentinel]
        result["verdicts"][0]["rationale"] = sentinel
        ai_audit_35.ingest_result(self.db_path, result)

        same_run_later = ai_audit_35.export_pass(
            self.db_path, run_id=first["run_id"], pass_number=2, auditor_id="later-blind",
            perspective="independent", model_id="gpt-test", prompt_version="blind-v2",
        )
        later_run = ai_audit_35.create_run(
            self.db_path, auditors=["next-run-blind"], model_id="gpt-test",
            prompt_version="blind-v3", perspective="independent", subject_ids=[Q1, Q2],
        )
        later_run_bundle = ai_audit_35.export_pass(self.db_path, pass_id=later_run["passes"][0]["id"])
        self.assertNotIn(sentinel, json.dumps(same_run_later, ensure_ascii=False))
        self.assertNotIn(sentinel, json.dumps(later_run_bundle, ensure_ascii=False))
        self.assertNotIn("ai_audit", json.dumps(later_run_bundle["source"], ensure_ascii=False))

        isolated_run = ai_audit_35.create_run(
            isolated, auditors=["isolated"], model_id="gpt-test", prompt_version="blind-v4",
            perspective="independent", subject_ids=[Q1, Q2],
        )
        isolated_bundle = ai_audit_35.export_pass(isolated, pass_id=isolated_run["passes"][0]["id"])
        self.assertNotIn(sentinel, json.dumps(isolated_bundle, ensure_ascii=False))

    def test_scope_mismatch_cannot_claim_convergence_and_duplicate_finding_cannot_inflate(self):
        run = ai_audit_35.create_run(self.db_path, subject_ids=[Q1, Q2], label="scope-convergence")
        all_pass = ai_audit_35.export_pass(
            self.db_path, run_id=run["run_id"], pass_number=1, auditor_id="all",
            perspective="independent", model_id="gpt-test", prompt_version="scope-math-v1",
        )
        listen_pass = ai_audit_35.export_pass(
            self.db_path, run_id=run["run_id"], pass_number=2, auditor_id="listen",
            perspective="transcript_alignment", model_id="gpt-test", prompt_version="scope-math-v1",
        )
        finding = {
            "subject_id": Q2, "category": "answer", "severity": "medium",
            "summary": "one valid occurrence", "detail": "synthetic",
            "identity": {"field": "answer", "issue": "scope-test"},
            "evidence": {"field": "answer.choice_number", "observed": 1, "expected": 2},
        }
        ai_audit_35.ingest_response(self.db_path, all_pass["pass_id"], self._custom_result(
            all_pass["pass_id"],
            [
                {"subject_id": Q1, "verdict": "clear", "confidence": 0.9, "rationale": "clear"},
                {"subject_id": Q2, "verdict": "finding", "confidence": 0.9, "rationale": "finding"},
            ],
            [finding],
        ))
        ai_audit_35.ingest_response(self.db_path, listen_pass["pass_id"], self._custom_result(
            listen_pass["pass_id"],
            [{"subject_id": Q1, "verdict": "clear", "confidence": 0.9, "rationale": "clear"}],
        ))

        global_report = ai_audit_35.consensus_report(self.db_path, run["run_id"])
        self.assertEqual(global_report["convergence"]["new_findings_latest_pass"], 0)
        self.assertFalse(global_report["convergence"]["latest_previous_scope_match"])
        self.assertFalse(global_report["convergence"]["converged"])
        q1 = ai_audit_35.summarize_question(self.db_path, Q1, run["run_id"])
        self.assertEqual(q1["convergence"]["completed_passes"], 2)
        self.assertTrue(q1["convergence"]["latest_previous_scope_match"])
        self.assertTrue(q1["convergence"]["converged"])
        q2 = ai_audit_35.summarize_question(self.db_path, Q2, run["run_id"])
        self.assertEqual(q2["convergence"]["completed_passes"], 1)
        self.assertFalse(q2["convergence"]["converged"])

        duplicate_pass = ai_audit_35.export_pass(
            self.db_path, run_id=run["run_id"], pass_number=3, auditor_id="duplicate",
            perspective="independent", model_id="gpt-test", prompt_version="scope-math-v1",
        )
        duplicate_result = self._custom_result(
            duplicate_pass["pass_id"],
            [
                {"subject_id": Q1, "verdict": "clear", "confidence": 0.9, "rationale": "clear"},
                {"subject_id": Q2, "verdict": "finding", "confidence": 0.9, "rationale": "duplicate"},
            ],
            [finding, dict(finding, summary="same fingerprint repeated")],
        )
        rejected = ai_audit_35.ingest_response(self.db_path, duplicate_pass["pass_id"], duplicate_result)
        self.assertEqual(rejected["status"], "invalid")
        self.assertIn("Duplicate finding fingerprint", rejected["validation_error"])
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM ai_audit_finding_occurrences o "
                    "JOIN ai_audit_passes p ON p.id=o.pass_id WHERE p.run_id=?",
                    (run["run_id"],),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM ai_audit_results WHERE pass_id=?", (duplicate_pass["pass_id"],)).fetchone()[0],
                0,
            )

    def test_attempt_failures_are_queryable_retryable_and_do_not_block_consensus(self):
        before = self._human_state()
        run = self._run(4)
        failed_pass, timeout_pass, invalid_pass, success_pass = run["passes"]

        failed = ai_audit_35.record_attempt_outcome(
            self.db_path, failed_pass["id"], "failed",
            error_code="provider_error", error_message="provider returned 503",
            evidence={"http_status": 503},
        )
        timed_out = ai_audit_35.record_attempt_outcome(
            self.db_path, timeout_pass["id"], "timed_out",
            error_code="deadline_exceeded", error_message="response exceeded 120 seconds",
            evidence={"timeout_seconds": 120},
        )
        bad_response = self._result(invalid_pass)
        bad_response["verdicts"] = []
        invalid = ai_audit_35.ingest_response(
            self.db_path, invalid_pass["id"], bad_response,
            evidence={"transport": "local-fixture"},
        )
        succeeded = ai_audit_35.ingest_response(
            self.db_path, success_pass["id"], self._result(success_pass, finding=True),
            evidence={"transport": "local-fixture"},
        )

        self.assertEqual((failed["attempt_number"], failed["status"]), (1, "failed"))
        self.assertEqual((timed_out["attempt_number"], timed_out["status"]), (1, "timed_out"))
        self.assertEqual(invalid["status"], "invalid")
        self.assertEqual(invalid["attempt"]["attempt_number"], 1)
        self.assertIsNone(invalid["result"])
        self.assertIn("exactly one", invalid["validation_error"])
        self.assertEqual(succeeded["status"], "succeeded")
        self.assertEqual(succeeded["attempt"]["attempt_number"], 1)

        consensus = ai_audit_35.consensus_report(self.db_path, run["run_id"])
        self.assertEqual(consensus["completed_passes"], 1)
        self.assertEqual(consensus["findings"][0]["consensus_count"], 1)

        retry = ai_audit_35.ingest_response(
            self.db_path, invalid_pass["id"], self._result(invalid_pass),
            evidence={"retry_reason": "fixed structured output"},
        )
        self.assertEqual(retry["status"], "succeeded")
        self.assertEqual(retry["attempt"]["attempt_number"], 2)
        attempts = ai_audit_35.list_attempts(self.db_path, pass_id=invalid_pass["id"])
        self.assertEqual([item["status"] for item in attempts], ["invalid", "succeeded"])
        self.assertEqual(attempts[0]["error_code"], "invalid_structured_response")
        self.assertEqual(attempts[0]["evidence"], {"transport": "local-fixture"})
        self.assertEqual(attempts[1]["evidence"], {"retry_reason": "fixed structured output"})

        status = ai_audit_35.status_report(self.db_path, run["run_id"])
        indexed = {item["id"]: item for item in status["passes"]}
        self.assertEqual(indexed[failed_pass["id"]]["latest_attempt_status"], "failed")
        self.assertEqual(indexed[timeout_pass["id"]]["latest_attempt_status"], "timed_out")
        self.assertEqual(indexed[invalid_pass["id"]]["attempt_status_counts"]["invalid"], 1)
        self.assertEqual(indexed[invalid_pass["id"]]["attempt_status_counts"]["succeeded"], 1)
        self.assertEqual(indexed[invalid_pass["id"]]["state"], "complete")
        self.assertTrue(indexed[failed_pass["id"]]["retryable"])
        self.assertEqual(status["latest_run"]["attempt_status_counts"]["failed"], 1)
        self.assertEqual(status["latest_run"]["attempt_status_counts"]["timed_out"], 1)
        self.assertEqual(status["latest_run"]["attempt_status_counts"]["invalid"], 1)
        self.assertEqual(status["latest_run"]["attempt_status_counts"]["succeeded"], 2)
        self.assertEqual(ai_audit_35.consensus_report(self.db_path, run["run_id"])["completed_passes"], 2)
        self.assertEqual(self._human_state(), before)

        with closing(sqlite3.connect(self.db_path)) as db:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE ai_audit_attempts SET status='failed' WHERE pass_id=?", (invalid_pass["id"],))
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("DELETE FROM ai_audit_attempts WHERE pass_id=?", (invalid_pass["id"],))

    def test_success_result_and_attempt_are_atomic(self):
        run = self._run(1)
        one = run["passes"][0]
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "CREATE TRIGGER test_abort_attempt BEFORE INSERT ON ai_audit_attempts "
                "BEGIN SELECT RAISE(ABORT, 'fixture attempt failure'); END"
            )
            db.commit()
        with self.assertRaises(sqlite3.DatabaseError):
            ai_audit_35.ingest_response(self.db_path, one["id"], self._result(one))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_audit_results WHERE pass_id=?", (one["id"],)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_audit_attempts WHERE pass_id=?", (one["id"],)).fetchone()[0], 0)

    def test_subject_scoped_status_excludes_passes_that_never_received_that_question(self):
        run = ai_audit_35.create_run(self.db_path, label="subject-scope")
        listening = ai_audit_35.export_pass(
            self.db_path, run_id=run["run_id"], pass_number=1,
            auditor_id="listen-only", perspective="transcript_alignment",
            model_id="gpt-test", prompt_version="scope-v1",
        )
        independent = ai_audit_35.export_pass(
            self.db_path, run_id=run["run_id"], pass_number=2,
            auditor_id="all-questions", perspective="independent",
            model_id="gpt-test", prompt_version="scope-v1",
        )
        ai_audit_35.record_attempt_outcome(
            self.db_path, listening["pass_id"], "timed_out",
            error_code="deadline", error_message="listening audit timeout",
            evidence={"scope": "listening"},
        )
        ai_audit_35.record_attempt_outcome(
            self.db_path, independent["pass_id"], "failed",
            error_code="provider", error_message="general audit failed",
            evidence={"scope": "all"},
        )

        reading = ai_audit_35.status_report(self.db_path, run["run_id"], subject_id=Q2)
        self.assertEqual([item["id"] for item in reading["passes"]], [independent["pass_id"]])
        self.assertEqual(reading["latest_run"]["attempt_status_counts"]["timed_out"], 0)
        self.assertEqual(reading["latest_run"]["attempt_status_counts"]["failed"], 1)
        self.assertEqual(reading["latest_run"]["subject_id"], Q2)

        listening_status = ai_audit_35.status_report(self.db_path, run["run_id"], subject_id=Q1)
        self.assertEqual(
            [item["id"] for item in listening_status["passes"]],
            [listening["pass_id"], independent["pass_id"]],
        )
        self.assertEqual(listening_status["latest_run"]["attempt_status_counts"]["timed_out"], 1)
        self.assertEqual(listening_status["latest_run"]["attempt_status_counts"]["failed"], 1)

    def test_run_subject_selection_is_immutable_ordered_and_perspective_scoped(self):
        default_run = self._run(1)
        default_bundle = ai_audit_35.export_pass(
            self.db_path, pass_id=default_run["passes"][0]["id"]
        )
        self.assertEqual([item["id"] for item in default_bundle["subjects"]], [Q1, Q2])
        self.assertEqual(default_run["subject_ids"], [Q1, Q2])

        selected = ai_audit_35.create_run(
            self.db_path, subject_ids=[Q2, Q1], label="selected-two"
        )
        self.assertEqual(selected["subject_ids"], [Q1, Q2])
        self.assertEqual(selected["subject_count"], 2)
        listening = ai_audit_35.export_pass(
            self.db_path, run_id=selected["run_id"], pass_number=1,
            auditor_id="selected-listening", perspective="transcript_alignment",
            model_id="gpt-test", prompt_version="scope-v2",
        )
        self.assertEqual([item["id"] for item in listening["subjects"]], [Q1])

        later = ai_audit_35.create_run(
            self.db_path, auditors=["targeted-reading"], model_id="gpt-test",
            prompt_version="scope-v2", perspective="independent",
            subject_ids=[Q2], label="targeted-reread",
        )
        later_bundle = ai_audit_35.export_pass(
            self.db_path, pass_id=later["passes"][0]["id"]
        )
        self.assertEqual([item["id"] for item in later_bundle["subjects"]], [Q2])
        status = ai_audit_35.status_report(self.db_path, later["run_id"])
        self.assertEqual(status["latest_run"]["subject_ids"], [Q2])
        self.assertEqual(status["latest_run"]["subject_count"], 1)

        with self.assertRaises(ai_audit_35.AuditError):
            ai_audit_35.create_run(self.db_path, subject_ids=[])
        with self.assertRaises(ai_audit_35.AuditError):
            ai_audit_35.create_run(self.db_path, subject_ids=[Q1, Q1])
        with self.assertRaises(ai_audit_35.AuditError):
            ai_audit_35.create_run(self.db_path, subject_ids=["035-I-R-999"])

    def test_multi_run_question_history_preserves_old_findings_without_claiming_resolution(self):
        first = ai_audit_35.create_run(
            self.db_path, auditors=["first-a", "first-b"], model_id="gpt-test",
            prompt_version="history-v1", perspective="independent",
            subject_ids=[Q1, Q2], label="first-round",
        )
        ai_audit_35.ingest_result(
            self.db_path,
            self._result(first["passes"][0], finding=True, finding_subject=Q2, summary="Q2 첫 발견"),
        )
        ai_audit_35.ingest_result(
            self.db_path,
            self._result(first["passes"][1], finding=False),
        )
        first_summary = ai_audit_35.summarize_question(self.db_path, Q2, first["run_id"])
        self.assertTrue(first_summary["disagreement"])
        self.assertEqual(first_summary["finding"], 1)

        second = ai_audit_35.create_run(
            self.db_path, auditors=["second-targeted"], model_id="gpt-test",
            prompt_version="history-v2", perspective="independent",
            subject_ids=[Q2], label="targeted-reread",
        )
        ai_audit_35.ingest_result(
            self.db_path,
            self._result(second["passes"][0], completed=[Q2], finding=False),
        )
        unrelated = ai_audit_35.create_run(
            self.db_path, auditors=["later-listening"], model_id="gpt-test",
            prompt_version="history-v3", perspective="transcript_alignment",
            subject_ids=[Q1], label="later-unrelated-listening",
        )
        ai_audit_35.ingest_result(
            self.db_path,
            self._result(unrelated["passes"][0], completed=[Q1], finding=False),
        )

        current_q2 = ai_audit_35.summarize_question(self.db_path, Q2)
        self.assertEqual(current_q2["run_id"], second["run_id"])
        current_q2_status = ai_audit_35.status_report(self.db_path, subject_id=Q2)
        self.assertEqual(current_q2_status["latest_run"]["run_id"], second["run_id"])
        latest_per_question = ai_audit_35.summarize_all_questions(self.db_path)
        self.assertIn(Q1, latest_per_question)
        self.assertIn(Q2, latest_per_question)
        self.assertEqual(latest_per_question[Q2]["run_id"], second["run_id"])
        self.assertIsNone(latest_per_question["run_id"])
        self.assertEqual(latest_per_question["selection_mode"], "latest_per_question")
        self.assertEqual(
            set(latest_per_question["source_run_ids"]),
            {second["run_id"], unrelated["run_id"]},
        )
        self.assertIsNone(latest_per_question["latest_run"])
        self.assertEqual(latest_per_question["convergence"]["mode"], "latest_per_question")
        self.assertEqual(
            set(latest_per_question["convergence"]["run_ids"]),
            {second["run_id"], unrelated["run_id"]},
        )

        # Reopen SQLite independently before reading the persisted audit history.
        with closing(sqlite3.connect(self.db_path)) as reopened:
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM ai_audit_runs").fetchone()[0], 3)
        history = ai_audit_35.question_audit_history(self.db_path, Q2)
        self.assertEqual(history["order"], "newest_first")
        self.assertEqual(history["run_count"], 2)
        self.assertEqual(history["audit_count"], 3)
        self.assertEqual(history["verdict_counts"], {"clear": 2, "finding": 1, "uncertain": 0})
        self.assertEqual(history["disagreement_run_count"], 1)
        self.assertEqual(history["latest_run_id"], second["run_id"])
        self.assertEqual(history["latest_run_finding_count"], 0)
        self.assertEqual(history["latest_run_open_findings"], [])
        self.assertEqual([item["run_id"] for item in history["runs"]], [second["run_id"], first["run_id"]])
        self.assertIsNotNone(history["runs"][0]["summary"])
        self.assertTrue(history["runs"][1]["summary"]["disagreement"])
        self.assertEqual(history["runs"][1]["summary"]["finding"], 1)
        self.assertEqual(len(history["historical_findings"]), 1)
        self.assertEqual(
            history["historical_findings"][0]["latest_state"],
            "not_reproduced_in_latest_run",
        )

        # The old completed run remains directly reviewable after the newer run exists.
        old_again = ai_audit_35.summarize_question(self.db_path, Q2, first["run_id"])
        self.assertEqual(old_again["finding"], 1)
        self.assertTrue(old_again["disagreement"])

        history_out = io.StringIO()
        with redirect_stdout(history_out):
            self.assertEqual(ai_audit_35.main([
                "--db", str(self.db_path), "history", "--subject-id", Q2,
            ]), 0)
        self.assertEqual(json.loads(history_out.getvalue())["audit_count"], 3)

    def test_history_tracks_true_first_seen_and_does_not_treat_incomplete_latest_as_nonreproduction(self):
        oldest = ai_audit_35.create_run(
            self.db_path, auditors=["oldest"], model_id="gpt-test",
            prompt_version="history-edge-v1", perspective="independent",
            subject_ids=[Q2], label="oldest-finding",
        )
        ai_audit_35.ingest_result(
            self.db_path,
            self._result(oldest["passes"][0], completed=[Q2], finding=True, finding_subject=Q2),
        )
        newer = ai_audit_35.create_run(
            self.db_path, auditors=["newer-a", "newer-b"], model_id="gpt-test",
            prompt_version="history-edge-v2", perspective="independent",
            subject_ids=[Q2], label="newer-finding",
        )
        for pass_info in newer["passes"]:
            ai_audit_35.ingest_result(
                self.db_path,
                self._result(pass_info, completed=[Q2], finding=True, finding_subject=Q2),
            )
        incomplete = ai_audit_35.create_run(
            self.db_path, auditors=["incomplete-a", "incomplete-b"], model_id="gpt-test",
            prompt_version="history-edge-v3", perspective="independent",
            subject_ids=[Q2], label="incomplete-latest",
        )
        ai_audit_35.ingest_result(
            self.db_path,
            self._result(incomplete["passes"][0], completed=[Q2], finding=False),
        )

        history = ai_audit_35.question_audit_history(self.db_path, Q2)
        with closing(sqlite3.connect(self.db_path)) as db:
            raw_occurrences = db.execute(
                "SELECT COUNT(*) FROM ai_audit_finding_occurrences o "
                "JOIN ai_audit_findings f ON f.fingerprint=o.fingerprint WHERE f.subject_id=?",
                (Q2,),
            ).fetchone()[0]
            raw_seen_runs = db.execute(
                "SELECT COUNT(DISTINCT p.run_id) FROM ai_audit_finding_occurrences o "
                "JOIN ai_audit_passes p ON p.id=o.pass_id "
                "JOIN ai_audit_findings f ON f.fingerprint=o.fingerprint WHERE f.subject_id=?",
                (Q2,),
            ).fetchone()[0]
            partial_counts = db.execute(
                "SELECT COUNT(*),SUM(CASE WHEN r.id IS NOT NULL THEN 1 ELSE 0 END) "
                "FROM ai_audit_passes p LEFT JOIN ai_audit_results r ON r.pass_id=p.id WHERE p.run_id=?",
                (incomplete["run_id"],),
            ).fetchone()
        self.assertEqual(raw_occurrences, 3)
        self.assertEqual(raw_seen_runs, 2)
        self.assertEqual(tuple(partial_counts), (2, 1))
        self.assertEqual(history["latest_run_id"], incomplete["run_id"])
        self.assertFalse(history["latest_run_complete"])
        self.assertEqual(history["latest_completed_run_id"], newer["run_id"])
        self.assertIsNone(history["latest_run_finding_count"])
        self.assertIsNone(history["latest_run_open_findings"])
        finding = history["historical_findings"][0]
        self.assertEqual(finding["first_seen_run_id"], oldest["run_id"])
        self.assertEqual(finding["latest_seen_run_id"], newer["run_id"])
        self.assertEqual(finding["seen_run_count"], 2)
        self.assertEqual(finding["occurrence_count"], 3)
        self.assertEqual(
            finding["occurrences_by_run"],
            [
                {"run_id": newer["run_id"], "occurrences": 2},
                {"run_id": oldest["run_id"], "occurrences": 1},
            ],
        )
        self.assertEqual(finding["latest_state"], "latest_run_incomplete")
        partial = ai_audit_35.summarize_question(self.db_path, Q2, incomplete["run_id"])
        self.assertEqual(partial["cross_run_convergence"], "stable")

    def test_history_marks_reappearance_after_completed_nonreproduction_without_claiming_resolution(self):
        first = ai_audit_35.create_run(
            self.db_path, auditors=["first"], model_id="gpt-test",
            prompt_version="reappear-v1", perspective="independent",
            subject_ids=[Q2], label="finding-first",
        )
        ai_audit_35.ingest_result(
            self.db_path,
            self._result(first["passes"][0], completed=[Q2], finding=True, finding_subject=Q2),
        )
        absent = ai_audit_35.create_run(
            self.db_path, auditors=["absent"], model_id="gpt-test",
            prompt_version="reappear-v2", perspective="independent",
            subject_ids=[Q2], label="completed-nonreproduction",
        )
        ai_audit_35.ingest_result(
            self.db_path,
            self._result(absent["passes"][0], completed=[Q2], finding=False),
        )
        latest = ai_audit_35.create_run(
            self.db_path, auditors=["latest"], model_id="gpt-test",
            prompt_version="reappear-v3", perspective="independent",
            subject_ids=[Q2], label="finding-reappears",
        )
        ai_audit_35.ingest_result(
            self.db_path,
            self._result(latest["passes"][0], completed=[Q2], finding=True, finding_subject=Q2),
        )

        history = ai_audit_35.question_audit_history(self.db_path, Q2)
        finding = history["historical_findings"][0]
        self.assertEqual(finding["seen_run_ids"], [latest["run_id"], first["run_id"]])
        self.assertEqual(finding["occurrence_count"], 2)
        self.assertEqual(finding["latest_state"], "reappeared_in_latest_run")
        self.assertTrue(finding["reappeared_after_nonreproduction"])
        self.assertEqual(finding["intervening_nonreproduction_run_ids"], [absent["run_id"]])
        self.assertNotIn("resolved", json.dumps(finding, ensure_ascii=False).lower())
        self.assertEqual(history["runs"][1]["run_id"], absent["run_id"])
        self.assertEqual(history["runs"][1]["summary"]["findings"], [])

    def test_checkpoint_resume_final_import_are_append_only(self):
        before = self._human_state()
        run = self._run(1)
        one = run["passes"][0]
        checkpoint = self._result(one, kind="checkpoint", completed=[Q1], finding=True, state={"cursor": 1})
        checkpoint["contract_version"] = ai_audit_35.CONTRACT_VERSION
        saved = ai_audit_35.ingest_checkpoint(self.db_path, checkpoint)
        self.assertEqual(saved["sequence"], 1)
        resumed = ai_audit_35.export_pass(self.db_path, pass_id=one["id"], resume=True)
        self.assertEqual([item["id"] for item in resumed["source"]["questions"]], [Q2])
        self.assertEqual(resumed["resume"]["completed_subject_ids"], [Q1])
        self.assertEqual(resumed["resume"]["state"], {"cursor": 1})

        shrinking = self._result(one, kind="checkpoint", completed=[Q2], finding=False)
        with self.assertRaises(ai_audit_35.AuditError):
            ai_audit_35.ingest_checkpoint(self.db_path, shrinking)

        final = self._result(one, finding=True)
        final["contract_version"] = ai_audit_35.CONTRACT_VERSION
        first = ai_audit_35.ingest_result(self.db_path, final)
        second = ai_audit_35.ingest_result(self.db_path, final)
        self.assertFalse(first["reused_existing_result"])
        self.assertTrue(second["reused_existing_result"])
        direct_attempts = ai_audit_35.list_attempts(self.db_path, pass_id=one["id"])
        self.assertEqual([(item["attempt_number"], item["status"]) for item in direct_attempts], [(1, "succeeded")])
        self.assertEqual(self._human_state(), before)
        with closing(sqlite3.connect(self.db_path)) as db:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE ai_audit_passes SET perspective='changed' WHERE id=?", (one["id"],))
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("DELETE FROM ai_audit_results WHERE pass_id=?", (one["id"],))

    def test_resumed_final_merges_checkpoint_after_reopen_and_rejects_stale_resume(self):
        run = self._run(1)
        one = run["passes"][0]
        checkpoint = self._result(
            one, kind="checkpoint", completed=[Q1], finding=True, state={"cursor": 1}
        )
        checkpoint["contract_version"] = ai_audit_35.CONTRACT_VERSION
        saved = ai_audit_35.ingest_checkpoint(self.db_path, checkpoint)

        # Every call reopens the database, matching process interruption/resume persistence.
        resumed = ai_audit_35.export_pass(self.db_path, pass_id=one["id"], resume=True)
        self.assertEqual([item["id"] for item in resumed["subjects"]], [Q2])
        self.assertEqual(resumed["resume"]["sequence"], saved["sequence"])
        self.assertEqual(resumed["resume"]["checkpoint_sha256"], saved["checkpoint_sha256"])
        resumed_final = self._result(one, completed=[Q2], finding=False)
        resumed_final["contract_version"] = ai_audit_35.CONTRACT_VERSION
        resumed_final["resume_sequence"] = resumed["resume"]["sequence"]
        resumed_final["resume_checkpoint_sha256"] = resumed["resume"]["checkpoint_sha256"]
        imported = ai_audit_35.ingest_response(self.db_path, one["id"], resumed_final)
        self.assertEqual(imported["status"], "succeeded")
        self.assertEqual(ai_audit_35.summarize_question(self.db_path, Q1, run["run_id"])["finding"], 1)
        self.assertEqual(ai_audit_35.summarize_question(self.db_path, Q2, run["run_id"])["clear"], 1)

        stale_run = self._run(1)
        stale_pass = stale_run["passes"][0]
        first_checkpoint = self._result(
            stale_pass, kind="checkpoint", completed=[Q1], finding=False, state={"cursor": 1}
        )
        first_checkpoint["contract_version"] = ai_audit_35.CONTRACT_VERSION
        ai_audit_35.ingest_checkpoint(self.db_path, first_checkpoint)
        stale_bundle = ai_audit_35.export_pass(self.db_path, pass_id=stale_pass["id"], resume=True)
        second_checkpoint = self._result(
            stale_pass, kind="checkpoint", completed=[Q1], finding=False, state={"cursor": 2}
        )
        second_checkpoint["contract_version"] = ai_audit_35.CONTRACT_VERSION
        ai_audit_35.ingest_checkpoint(self.db_path, second_checkpoint)
        stale_final = self._result(stale_pass, completed=[Q2], finding=False)
        stale_final["contract_version"] = ai_audit_35.CONTRACT_VERSION
        stale_final["resume_sequence"] = stale_bundle["resume"]["sequence"]
        stale_final["resume_checkpoint_sha256"] = stale_bundle["resume"]["checkpoint_sha256"]
        rejected = ai_audit_35.ingest_response(self.db_path, stale_pass["id"], stale_final)
        self.assertEqual(rejected["status"], "invalid")
        self.assertIn("stale", rejected["validation_error"])
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM ai_audit_results WHERE pass_id=?", (stale_pass["id"],)).fetchone()[0],
                0,
            )

    def test_invalid_structured_result_fails_closed(self):
        run = self._run(1)
        one = run["passes"][0]
        bad_hash = self._result(one)
        bad_hash["input_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            ai_audit_35.ingest_result(self.db_path, bad_hash)
        inconsistent = self._result(one, finding=True)
        inconsistent["verdicts"][0]["verdict"] = "clear"
        with self.assertRaises(ValueError):
            ai_audit_35.ingest_result(self.db_path, inconsistent)
        incomplete = self._result(one, completed=[Q1])
        with self.assertRaises(ValueError):
            ai_audit_35.ingest_result(self.db_path, incomplete)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_audit_results").fetchone()[0], 0)

    def test_cli_create_export_and_status(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = ai_audit_35.main(
                [
                    "--db", str(self.db_path), "create-run", "--auditor", "cli-agent",
                    "--model-id", "cli-model", "--prompt-version", "cli-prompt",
                ]
            )
        self.assertEqual(code, 0)
        created = json.loads(out.getvalue())
        pass_id = created["passes"][0]["id"]
        export_path = Path(self.temp.name) / "pass.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                ai_audit_35.main(
                    ["--db", str(self.db_path), "export-pass", "--pass-id", pass_id, "--output", str(export_path)]
                ),
                0,
            )
        exported = json.loads(export_path.read_text(encoding="utf-8"))
        self.assertEqual((exported["model_id"], exported["prompt_version"]), ("cli-model", "cli-prompt"))
        status = ai_audit_35.status_report(self.db_path, created["run_id"])
        self.assertEqual(status["passes"][0]["state"], "pending")

    def test_cli_invalid_retry_and_timeout_outcomes_are_auditable(self):
        run = self._run(2)
        retry_pass, timeout_pass = run["passes"]
        invalid_path = Path(self.temp.name) / "invalid-response.txt"
        invalid_path.write_text("{not valid json", encoding="utf-8")

        invalid_out = io.StringIO()
        with redirect_stdout(invalid_out):
            invalid_code = ai_audit_35.main([
                "--db", str(self.db_path), "import-result", "--pass-id", retry_pass["id"],
                "--input", str(invalid_path),
            ])
        self.assertEqual(invalid_code, 2)
        invalid_saved = json.loads(invalid_out.getvalue())
        self.assertEqual(invalid_saved["status"], "invalid")
        self.assertEqual(invalid_saved["attempt"]["attempt_number"], 1)

        corrected_path = Path(self.temp.name) / "corrected-response.json"
        corrected_path.write_text(
            json.dumps(self._result(retry_pass), ensure_ascii=False), encoding="utf-8"
        )
        corrected_out = io.StringIO()
        with redirect_stdout(corrected_out):
            corrected_code = ai_audit_35.main([
                "--db", str(self.db_path), "import-pass", "--input", str(corrected_path),
            ])
        self.assertEqual(corrected_code, 0)
        corrected_saved = json.loads(corrected_out.getvalue())
        self.assertEqual(corrected_saved["status"], "succeeded")
        self.assertEqual(corrected_saved["attempt"]["attempt_number"], 2)

        timeout_out = io.StringIO()
        with redirect_stdout(timeout_out):
            timeout_code = ai_audit_35.main([
                "--db", str(self.db_path), "record-outcome", "--pass-id", timeout_pass["id"],
                "--status", "timed_out", "--error-code", "deadline_exceeded",
                "--error-message", "provider timeout", "--evidence-json", '{"timeout_seconds":60}',
            ])
        self.assertEqual(timeout_code, 0)
        self.assertEqual(json.loads(timeout_out.getvalue())["status"], "timed_out")

        attempts_out = io.StringIO()
        with redirect_stdout(attempts_out):
            attempts_code = ai_audit_35.main([
                "--db", str(self.db_path), "attempts", "--pass-id", retry_pass["id"],
            ])
        self.assertEqual(attempts_code, 0)
        attempts = json.loads(attempts_out.getvalue())
        self.assertEqual([item["status"] for item in attempts], ["invalid", "succeeded"])
        self.assertEqual(attempts[0]["raw_response"], "{not valid json")
        status = ai_audit_35.status_report(self.db_path, run["run_id"])
        indexed = {item["id"]: item for item in status["passes"]}
        self.assertEqual(indexed[retry_pass["id"]]["latest_attempt_status"], "succeeded")
        self.assertEqual(indexed[timeout_pass["id"]]["latest_attempt_status"], "timed_out")

    def test_cli_json_rendering_escapes_only_unencodable_console_evidence(self):
        class CP949Stream:
            encoding = "cp949"

        rendered = ai_audit_35._json_for_stream(
            {"raw_response": "\ufeff한글 증거", "emoji": "😀"}, CP949Stream()
        )
        self.assertIn("한글 증거", rendered)
        self.assertIn("\\ufeff", rendered)
        self.assertIn("\\U0001f600", rendered)
        self.assertNotIn("\ufeff", rendered)


if __name__ == "__main__":
    unittest.main()
