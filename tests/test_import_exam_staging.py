"""The 36th-session append gate must fail closed before touching PostgreSQL."""

import copy
import hashlib
import unittest
from pathlib import Path

from scripts import import_exam_staging as importer


class ImportExamStagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = importer.CORPUS / "36th"
        files = (
            ("36th-TOPIK-I-Combined-Test-Paper.pdf", "test_paper"),
            ("36th-TOPIK-I-Answer-Keys.pdf", "answer_key"),
            ("36th-TOPIK-I-Listening-Transcript.pdf", "listening_transcript"),
            ("36th-TOPIK-I-Listening-Audio.mp3", "listening_audio"),
        )
        cls.sources = [
            {
                "relative_path": (Path("topik-past-papers") / "36th" / name).as_posix(),
                "kind": kind,
                "sha256": importer.sha256_file(root / name),
                "byte_size": (root / name).stat().st_size,
            }
            for name, kind in files
        ]
        cls.paper, cls.answer, cls.transcript = (
            cls.sources[i]["relative_path"] for i in (0, 1, 2)
        )

    def setUp(self):
        questions = []
        for number in range(1, 71):
            listening = number <= 30
            section = "listening" if listening else "reading"
            questions.append({
                "id": f"036-I-{'L' if listening else 'R'}-{number:03d}",
                "section": section,
                "exam_number": number,
                "answer_key_number": number,
                "group_id": f"036-I-{section}-all",
                "source_relative_path": self.paper,
                "source_pdf_page": 4 if listening else 11,
                "points": 4 if listening and number <= 10 else
                          3 if listening and number <= 30 else
                          2 if number <= 50 else 3,
                "stem": f"Source PDF question {number}",
                "raw_question_text": f"Source PDF question {number}",
                "requires_image": False,
                "images": [],
                "choices": [{"number": n, "text": f"choice {n}"} for n in range(1, 5)],
                "answer": {"choice_number": 1, "source_pdf_page": 1 if listening else 2},
                "transcript": (
                    {"dialogue_text": f"Transcript {number}", "source_relative_path": self.transcript,
                     "source_pdf_page": 1, "warnings": []} if listening else None
                ),
            })
        # Only the source-backed totals are relevant to import contract; the
        # text here is synthetic and never staged or imported.
        for section, targets in (
            ("listening", [q for q in questions if q["section"] == "listening"]),
            ("reading", [q for q in questions if q["section"] == "reading"]),
        ):
            for q in targets:
                q["points"] = 3
            for q in targets[:10 if section == "listening" else 10]:
                q["points"] = 4 if section == "listening" else 2
            if section == "reading":
                # 40 x 3 - 10 x 1 = 110. Make 10 more rows 2 points.
                for q in targets[10:20]:
                    q["points"] = 2
        self.staging = {
            "exam": {"id": "036-I-B", "session": 36, "level": "I", "booklet": "B"},
            "sources": copy.deepcopy(self.sources),
            "groups": [
                {"id": "036-I-listening-all", "section": "listening",
                 "first_exam_number": 1, "last_exam_number": 30,
                 "instruction": "Listening", "passage_text": ""},
                {"id": "036-I-reading-all", "section": "reading",
                 "first_exam_number": 31, "last_exam_number": 70,
                 "instruction": "Reading", "passage_text": ""},
            ],
            "questions": questions,
            "warnings": [],
            "extraction_version": "test-only",
        }

    def test_valid_staging_has_70_questions_and_200_points(self):
        report = importer.validate(self.staging)
        self.assertEqual(report["status"], "validated")
        self.assertEqual(report["questions"], 70)
        self.assertEqual(report["choices"], 280)
        self.assertEqual(report["answers"], 70)
        self.assertEqual(report["transcripts"], 30)
        self.assertEqual(report["points_by_section"], {"listening": 100, "reading": 100})

    def test_reading_local_answer_number_offset_is_forbidden(self):
        self.staging["questions"][30]["answer_key_number"] = 1
        with self.assertRaisesRegex(importer.ImportBlocked, "global"):
            importer.validate(self.staging)

    def test_missing_question_or_duplicate_rejected(self):
        self.staging["questions"][69]["id"] = "036-I-R-069"
        with self.assertRaisesRegex(importer.ImportBlocked, "question IDs"):
            importer.validate(self.staging)

    def test_bad_source_hash_blocks(self):
        self.staging["sources"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(importer.ImportBlocked, "source hash"):
            importer.validate(self.staging)

    def test_missing_transcript_blocks(self):
        self.staging["questions"][0]["transcript"] = None
        with self.assertRaisesRegex(importer.ImportBlocked, "listening transcript"):
            importer.validate(self.staging)

    def test_blocking_warnings_are_never_applied(self):
        self.staging["warnings"] = [{"severity": "blocking", "code": "source_ambiguity",
                                      "message": "source ambiguity"}]
        self.assertEqual(importer.validate(self.staging)["status"], "blocked")

    def test_unknown_warning_format_fails_closed_including_nested(self):
        self.staging["warnings"] = ["CRITICAL: source mismatch"]
        with self.assertRaisesRegex(importer.ImportBlocked, "malformed"):
            importer.validate(self.staging)
        self.staging["warnings"] = []
        self.staging["questions"][0]["transcript"]["warnings"] = [
            {"severity": "blocking", "code": "text_mismatch", "message": "unresolved"}
        ]
        self.assertEqual(importer.validate(self.staging)["status"], "blocked")

    def test_36th_namespace_does_not_collide_with_35th(self):
        self.assertEqual(importer.EXAM_ID, "036-I-B")
        self.assertTrue(all(q["id"].startswith("036-I-") for q in self.staging["questions"]))

    def test_pdf_first_import_does_not_claim_nonexistent_preview_agreement(self):
        from inspect import getsource
        code = getsource(importer.apply)
        self.assertIn("preview_and_pdf_agree) VALUES(?,?,?,?,0)", code)
        self.assertNotIn("preview_and_pdf_agree) VALUES(?,?,?,?,1)", code)


if __name__ == "__main__":
    unittest.main()
