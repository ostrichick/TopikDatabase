"""Regression checks for the 2026-10-08 source-backed 35-I correction manifest."""

from __future__ import annotations

import base64
import hashlib
import sqlite3
import unittest
from unittest.mock import patch

from scripts import correct_audit_findings_35 as correction
from scripts import finalize_blind_audit_35 as finalization
from src import ai_audit_35, pilot_35


class CorrectAuditFindings35Tests(unittest.TestCase):
    def test_manifest_matches_frozen_stage10_baseline_and_exact_scope(self):
        db = sqlite3.connect(pilot_35.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for qid, item in correction.QUESTION_CORRECTIONS.items():
                row = db.execute("SELECT stem FROM questions WHERE id=?", (qid,)).fetchone()
                self.assertIsNotNone(row, qid)
                self.assertEqual(row[0], item["before"], qid)
                self.assertNotEqual(item["before"], item["after"], qid)
            for gid, item in correction.GROUP_CORRECTIONS.items():
                row = db.execute(
                    f"SELECT {item['field']} FROM question_groups WHERE id=?", (gid,)
                ).fetchone()
                self.assertIsNotNone(row, gid)
                self.assertEqual(row[0], item["before"], gid)
                self.assertNotEqual(item["before"], item["after"], gid)
        finally:
            db.close()

        expected = {
            "035-I-L-025", "035-I-L-026", "035-I-L-027", "035-I-L-028", "035-I-L-029", "035-I-L-030",
            "035-I-R-031", "035-I-R-032", "035-I-R-033", "035-I-R-034", "035-I-R-035", "035-I-R-036",
            "035-I-R-037", "035-I-R-038", "035-I-R-039", "035-I-R-045", "035-I-R-057", "035-I-R-058",
            "035-I-R-067", "035-I-R-068",
        }
        self.assertEqual(set(correction.affected_question_ids()), expected)

    def test_target_state_fails_closed_on_partial_or_unknown_values(self):
        questions = {
            qid: {
                "group_id": correction.EXPECTED_GROUPS[qid],
                "stem": item["before"],
                "review_status": "verified",
            }
            for qid, item in correction.QUESTION_CORRECTIONS.items()
        }
        for qid in correction.affected_question_ids():
            questions.setdefault(qid, {
                "group_id": correction.EXPECTED_GROUPS[qid],
                "stem": "unchanged",
                "review_status": "verified",
            })
        groups = {
            gid: {"instruction": "", "passage_text": "", item["field"]: item["before"]}
            for gid, item in correction.GROUP_CORRECTIONS.items()
        }
        correction._validate_group_membership(questions)
        self.assertEqual(correction._target_state(questions, groups), "before")

        first_qid = next(iter(correction.QUESTION_CORRECTIONS))
        questions[first_qid]["stem"] = correction.QUESTION_CORRECTIONS[first_qid]["after"]
        with self.assertRaisesRegex(RuntimeError, "partially applied"):
            correction._target_state(questions, groups)

        questions[first_qid]["stem"] = "unexpected concurrent edit"
        with self.assertRaisesRegex(RuntimeError, "no longer matches"):
            correction._target_state(questions, groups)

        questions[first_qid]["stem"] = correction.QUESTION_CORRECTIONS[first_qid]["before"]
        questions[first_qid]["group_id"] = "unexpected-group"
        with self.assertRaisesRegex(RuntimeError, "group changed"):
            correction._validate_group_membership(questions)

    def test_correction_does_not_target_transcript_or_audio_content(self):
        self.assertTrue(all("transcript" not in item for item in correction.QUESTION_CORRECTIONS.values()))
        self.assertTrue(all(item["field"] in {"instruction", "passage_text"}
                            for item in correction.GROUP_CORRECTIONS.values()))

    def test_blind_audit_field_provenance_maps_listening_prompts_to_transcript(self):
        paper = {"relative_path": "paper.pdf", "page": 9}
        answer = {"relative_path": "answer.pdf", "page": 1}
        transcript = {"relative_path": "transcript.pdf", "page": 10}

        q25 = ai_audit_35._field_source_refs(
            "035-I-L-025", paper=paper, answer=answer, transcript=transcript
        )
        q24 = ai_audit_35._field_source_refs(
            "035-I-L-024", paper=paper, answer=answer, transcript=transcript
        )
        self.assertEqual(q25["stem"], transcript)
        self.assertEqual(q25["choices"], paper)
        self.assertEqual(q24["stem"], paper)
        self.assertEqual(q25["answer"], answer)

    def test_real_source_snapshot_cites_listening_stems_at_transcript_pages(self):
        source = ai_audit_35.create_source_snapshot(
            pilot_35.DB_PATH.resolve()
        )["snapshot"]
        by_number = {q["exam_number"]: q for q in source["questions"]}
        for number, transcript_page in ((25, 10), (26, 10), (27, 11),
                                        (28, 11), (29, 12), (30, 12)):
            question = by_number[number]
            stem_source = question["field_sources"]["stem"]
            self.assertTrue(stem_source["relative_path"].endswith(
                "35th-TOPIK-I-Listening-Transcript.pdf"))
            self.assertEqual(stem_source["page"], transcript_page)
            self.assertTrue(question["field_sources"]["choices"]["relative_path"].endswith(
                "35th-TOPIK-I-Papers.pdf"))

        for number in (24, 31, 35, 70):
            self.assertEqual(
                by_number[number]["field_sources"]["stem"],
                by_number[number]["source_refs"]["paper"],
            )

        for number, page in {
            9: 4, 10: 4, 20: 7, 21: 7,
            38: 12, 39: 12, 41: 13, 42: 13,
        }.items():
            question = by_number[number]
            self.assertEqual(question["field_sources"]["group_instruction"]["page"], page)
            self.assertGreater(question["source_refs"]["paper"]["page"], page)

    def test_blind_snapshot_embeds_exact_active_image_payloads(self):
        db = sqlite3.connect(pilot_35.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            snapshot = ai_audit_35._source_snapshot_payload(db)
            active = db.execute(
                "SELECT DISTINCT i.key,i.sha256,i.bytes FROM question_images qi "
                "JOIN images i ON i.key=qi.image_key ORDER BY i.key"
            ).fetchall()
        finally:
            db.close()

        assets = {item["key"]: item for item in snapshot["image_assets"]}
        self.assertEqual(set(assets), {row[0] for row in active})
        for key, expected_sha, expected_bytes in active:
            payload = base64.b64decode(assets[key]["bytes_base64"], validate=True)
            self.assertEqual(payload, expected_bytes)
            self.assertEqual(hashlib.sha256(payload).hexdigest(), expected_sha)
            self.assertEqual(assets[key]["sha256"], expected_sha)

    def test_final_approval_requires_all_70_clear_and_no_findings(self):
        ids = [
            *(f"035-I-L-{number:03d}" for number in range(1, 31)),
            *(f"035-I-R-{number:03d}" for number in range(31, 71)),
        ]
        expected = set(ids)
        response = {
            "contract_version": ai_audit_35.CONTRACT_VERSION,
            "pass_id": finalization.PASS_ID,
            "input_sha256": finalization.INPUT_SHA256,
            "completed_subject_ids": ids,
            "verdicts": [
                {"subject_id": subject_id, "verdict": "clear"}
                for subject_id in ids
            ],
            "findings": [],
        }
        finalization._validate_result(response, expected)
        response["verdicts"][0]["verdict"] = "uncertain"
        with self.assertRaisesRegex(RuntimeError, "finding or uncertain"):
            finalization._validate_result(response, expected)
        response["verdicts"][0]["verdict"] = "clear"
        response["findings"] = [{"subject_id": ids[0]}]
        with self.assertRaisesRegex(RuntimeError, "unresolved findings"):
            finalization._validate_result(response, expected)

    def test_atomic_final_approval_commits_once_and_preserves_transcripts(self):
        ids = [f"035-I-L-{n:03d}" for n in range(1, 31)] + [
            f"035-I-R-{n:03d}" for n in range(31, 71)
        ]
        response = {
            "contract_version": ai_audit_35.CONTRACT_VERSION,
            "pass_id": finalization.PASS_ID,
            "input_sha256": finalization.INPUT_SHA256,
            "completed_subject_ids": ids,
            "verdicts": [{"subject_id": qid, "verdict": "clear"} for qid in ids],
            "findings": [],
        }

        class FakeCursor:
            def __init__(self, rows=(), rowcount=0):
                self.rows = rows
                self.rowcount = rowcount

            def fetchone(self):
                return self.rows[0] if self.rows else None

            def fetchall(self):
                return self.rows

        class FakeAuditConnection:
            def __init__(self, *, corrupted_source=False, rejected_transcript=False,
                         missing_history=False):
                self.corrupted_source = corrupted_source
                self.rejected_transcript = rejected_transcript
                self.missing_history = missing_history
                self.statements = []
                self.commits = 0
                self.rollbacks = 0
                self.closed = False

            def execute(self, sql, params=()):
                self.statements.append(sql)
                if sql.startswith("SELECT r.snapshot_sha256"):
                    return FakeCursor([(
                        finalization.SOURCE_SNAPSHOT_SHA256,
                        finalization.INPUT_SHA256,
                        "independent",
                        __import__("json").dumps(response),
                    )])
                if sql.startswith("SELECT id,review_status FROM questions"):
                    return FakeCursor([(qid, "needs_manual_review" if i < 45 else "verified")
                                       for i, qid in enumerate(ids)])
                if sql.startswith("SELECT question_id,review_status FROM transcripts"):
                    statuses = [(qid, "verified") for qid in ids[:30]]
                    if self.rejected_transcript:
                        statuses[0] = (ids[0], "rejected")
                    return FakeCursor(statuses)
                if sql.startswith("UPDATE questions SET review_status"):
                    return FakeCursor([(qid,) for qid in ids[:45]], 45)
                if sql.startswith("INSERT INTO review_records"):
                    return FakeCursor(rowcount=44 if self.missing_history else 45)
                if sql.startswith("SELECT review_status,COUNT"):
                    return FakeCursor([("verified", 70)])
                return FakeCursor()

            def commit(self):
                self.commits += 1

            def rollback(self):
                self.rollbacks += 1

            def close(self):
                self.closed = True

        def exercise(**options):
            db = FakeAuditConnection(**options)
            with patch.object(finalization, "get_database_url", return_value="postgresql://mock"), \
                 patch.object(finalization, "PostgresAuditConnection", return_value=db), \
                 patch.object(ai_audit_35, "_source_snapshot_payload", return_value={"mock": "snapshot"}), \
                 patch.object(ai_audit_35, "_sha", return_value=(
                     "different" if options.get("corrupted_source") else
                     finalization.SOURCE_SNAPSHOT_SHA256
                 )):
                if options:
                    with self.assertRaises(RuntimeError):
                        finalization.apply()
                else:
                    result = finalization.apply()
                    self.assertEqual(result["approved_now_count"], 45)
                    self.assertTrue(result["atomic"])
            return db

        successful = exercise()
        self.assertEqual(successful.commits, 1)
        self.assertEqual(successful.rollbacks, 0)
        self.assertTrue(successful.closed)
        self.assertTrue(next(i for i, s in enumerate(successful.statements)
                             if s.startswith("LOCK TABLE")) <
                        next(i for i, s in enumerate(successful.statements)
                             if s.startswith("UPDATE questions")))
        self.assertFalse(any("UPDATE transcripts" in sql for sql in successful.statements))

        for options in (
            {"corrupted_source": True},
            {"rejected_transcript": True},
            {"missing_history": True},
        ):
            failed = exercise(**options)
            self.assertEqual(failed.commits, 0)
            self.assertEqual(failed.rollbacks, 1)
            self.assertTrue(failed.closed)


if __name__ == "__main__":
    unittest.main()
