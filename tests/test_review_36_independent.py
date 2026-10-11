"""36th historical PDF-audit evidence must be complete and source-bound."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import review_ui


class Independent36Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="topik-36-audit-ui-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "topik-past-papers/derived/036-I-B"
        self.archive.mkdir(parents=True)
        self.store = object.__new__(review_ui.ReviewStore)
        self.store.root = self.root
        self.store.exam_id = "036-I-B"

        self.v4 = {
            "schema_version": "topik-36-source-audit-v4", "input_sha256": "a" * 64,
            "counts": {"total": 70, "clear": 70, "finding": 0, "uncertain": 0},
            "verdicts": [
                {"subject_id": self.qid(i), "exam_number": i, "verdict": "clear",
                 "rationale": f"검수 당시 원본 {i}번과 대조"}
                for i in range(1, 71)
            ],
        }
        self.v5 = {
            "schema_version": "topik36-second-blind-pass-v1", "exam_id": "036-I-B",
            "v5_sha256": "b" * 64, "created_at_utc": "2026-10-09T08:17:38+00:00",
            "summary": {"assigned": 70, "reviewed": 70, "clear": 70, "finding": 0, "uncertain": 0},
            "per_question": [
                {"id": self.qid(i), "number": i, "auditor": "A" if i <= 35 else "B",
                 "verdict": "clear", "original_pdf_page": 1,
                 "source_answer_choice": 1, "source_points": 2}
                for i in range(1, 71)
            ],
        }

    @staticmethod
    def qid(n):
        return f"036-I-{'L' if n <= 30 else 'R'}-{n:03d}"

    def publish(self):
        members = []
        for file in ("blind-audit-pass2-a.json", "blind-audit-pass2-b.json"):
            is_a = file.endswith("-a.json")
            audit_rows = []
            for n in (range(1, 36) if is_a else range(36, 71)):
                if is_a:
                    audit_rows.append({"question_id": self.qid(n), "verdict": "clear", "issues": [],
                                       "source_answer_choice": 1, "source_answer_points": 2,
                                       "source_evidence": {"question_pdf": "paper.pdf", "question_pdf_page": 1},
                                       "checks_done": {"original_pdf_verified": True}})
                else:
                    audit_rows.append({"id": self.qid(n), "verdict": "clear", "issues": [],
                                       "official_answer": 1, "official_points": 2,
                                       "source_citation": "paper.pdf#page=1",
                                       "checks_done": {"original_pdf_verified": True}})
            body = json.dumps({"findings": audit_rows}, ensure_ascii=False).encode("utf-8")
            (self.archive / file).write_bytes(body)
            members.append({"file": file, "sha256": hashlib.sha256(body).hexdigest()})
        self.v5["reviewer_source_files"] = members
        manifest = []
        for key, file, payload in (("first", "blind-audit-v4.json", self.v4),
                                   ("second", "blind-audit-pass2-combined.json", self.v5)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            (self.archive / file).write_bytes(body)
            manifest.append((key, file, hashlib.sha256(body).hexdigest()))
        return manifest

    def inspect(self, manifest):
        with (patch.object(review_ui, "AUDIT36_ARCHIVES", manifest),
              patch.object(review_ui, "AUDIT36_V4_SHA", "a" * 64),
              patch.object(review_ui, "AUDIT36_V5_SHA", "b" * 64)):
            return self.store.get_independent_audit_comparison()

    def test_two_verified_passes_keep_the_model_unattributed(self):
        evidence = self.inspect(self.publish())
        self.assertEqual(evidence["exam_id"], "036-I-B")
        self.assertTrue(evidence["available"])
        self.assertEqual([p["label"] for p in evidence["passes"]],
                         ["1차 독립 감수", "2차 독립 감수"])
        self.assertTrue(all(p["available"] and not p["model"] for p in evidence["passes"]))
        self.assertEqual(len(evidence["questions"]), 70)
        self.assertEqual(evidence["disagreement_count"], 0)
        self.assertEqual(set(evidence["questions"]["1"]["audits"]), {"first", "second"})
        self.assertIn("원본 1번", evidence["questions"]["1"]["audits"]["first"]["summary"])
        self.assertIn("정답 1번", evidence["questions"]["1"]["audits"]["second"]["summary"])
        self.assertIn("검사 항목 1개", evidence["questions"]["1"]["audits"]["second"]["detail"])
        self.assertFalse(evidence["questions"]["1"]["audits"]["first"]["timestamp_verified"])
        self.assertTrue(evidence["questions"]["1"]["audits"]["second"]["timestamp_verified"])

    def test_missing_or_changed_source_is_not_reported_as_clear(self):
        manifest = self.publish()
        (self.archive / "blind-audit-pass2-b.json").write_bytes(b"changed")
        evidence = self.inspect(manifest)
        self.assertFalse(evidence["passes"][1]["available"])
        self.assertEqual(set(evidence["questions"]["1"]["audits"]), {"first"})

    def test_hash_valid_but_incomplete_question_ids_are_rejected(self):
        self.v4["verdicts"][-1]["subject_id"] = self.qid(69)
        evidence = self.inspect(self.publish())
        self.assertFalse(evidence["passes"][0]["available"])
        self.assertTrue(evidence["passes"][1]["available"])

    def test_missing_files_are_not_silent_clear_claims(self):
        evidence = self.inspect([("first", "blind-audit-v4.json", "f" * 64),
                                 ("second", "blind-audit-pass2-combined.json", "e" * 64)])
        self.assertFalse(evidence["available"])
        self.assertEqual(len(evidence["passes"]), 2)
        self.assertTrue(all(not p["available"] and p["reason"] for p in evidence["passes"]))


if __name__ == "__main__":
    unittest.main()
