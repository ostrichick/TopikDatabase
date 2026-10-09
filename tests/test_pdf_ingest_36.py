"""36th TOPIK I B PDF-first source validation and fail-closed regression tests."""

from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import pdf_ingest_36 as ingest


class SourceExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = ingest.extract()

    def test_source_counts_exam_provenance(self):
        data = self.data
        self.assertEqual(data["exam"], {"id": "036-I-B", "session": 36, "level": "I", "booklet": "B"})
        self.assertEqual(len(data["sources"]), 6)
        self.assertEqual(sum(x["manifest_verified"] for x in data["sources"]), 4)
        self.assertEqual(len(data["groups"]), 26)
        self.assertEqual(len(data["questions"]), 70)
        self.assertEqual(len({q["id"] for q in data["questions"]}), 70)
        self.assertEqual([q["exam_number"] for q in data["questions"]], list(range(1, 71)))
        self.assertEqual(sum(q["section"] == "listening" for q in data["questions"]), 30)
        self.assertEqual(sum(q["section"] == "reading" for q in data["questions"]), 40)
        self.assertTrue(all(q["source_relative_path"].startswith("topik-past-papers/36th/") for q in data["questions"]))

    def test_answer_rows_points_are_global_for_reading(self):
        answers = ingest.answer_table(ingest.load_pdfs(ingest.source_documents()[0])[0]["answer_key"])
        self.assertEqual(set(answers), set(range(1, 71)))
        self.assertEqual(sum(v["points"] for v in answers.values()), 200)
        for q in self.data["questions"]:
            nr = q["exam_number"]
            self.assertEqual(q["answer_key_number"], nr)
            self.assertEqual(q["answer"]["choice_number"], answers[nr]["choice_number"])
            self.assertEqual(q["points"], answers[nr]["points"])
            self.assertEqual(q["answer"]["source_pdf_page"], 1 if nr <= 30 else 2)
        # The original 36th answer sheet explicitly numbers the reading section 31..70.
        self.assertEqual(answers[31]["choice_number"], 2)
        self.assertEqual(answers[70]["choice_number"], 2)

    def test_every_question_has_four_noninvented_slots_and_group(self):
        groups = {g["id"]: g for g in self.data["groups"]}
        for q in self.data["questions"]:
            self.assertEqual([c["number"] for c in q["choices"]], [1, 2, 3, 4])
            self.assertTrue(q["raw_question_text"])
            self.assertIn(q["group_id"], groups)
            self.assertLessEqual(groups[q["group_id"]]["first_exam_number"], q["exam_number"])
            self.assertGreaterEqual(groups[q["group_id"]]["last_exam_number"], q["exam_number"])
            self.assertTrue(q["requires_image"] or all(c["text"] for c in q["choices"]))
            self.assertGreaterEqual(q["points"], 2)
            self.assertLessEqual(q["points"], 4)
        self.assertIn("학교 앞에 새 카페", groups["036-I-R-49-50"]["passage_text"])
        self.assertIn("김지호 씨는 미용사", groups["036-I-R-67-68"]["passage_text"])
        self.assertIn("고르십시오", groups["036-I-L-01-04"]["instruction"])
        self.assertIn("고르십시오", groups["036-I-R-31-33"]["instruction"])
        self.assertNotIn("고르 십시오", groups["036-I-R-31-33"]["instruction"])

    def test_transcript_pair_alignment_and_provenance(self):
        qs = {q["exam_number"]: q for q in self.data["questions"]}
        self.assertEqual(sum("transcript" in q for q in qs.values()), 30)
        self.assertFalse(any("transcript" in qs[n] for n in range(31, 71)))
        for n in range(1, 31):
            record = qs[n]["transcript"]
            self.assertTrue(record["dialogue_text"])
            self.assertIn("36th-TOPIK-I-Listening-Transcript.pdf", record["source_relative_path"])
            self.assertTrue(record["warnings"])
        self.assertIn("\n남자 :", qs[1]["transcript"]["dialogue_text"])
        for n in (25, 27, 29):
            self.assertEqual(qs[n]["transcript"]["dialogue_text"], qs[n + 1]["transcript"]["dialogue_text"])
            self.assertEqual(qs[n]["transcript"]["source_pdf_page"], qs[n + 1]["transcript"]["source_pdf_page"])

    def test_pymupdf_visual_crop_is_source_backed(self):
        visual = [q for q in self.data["questions"] if q["requires_image"]]
        self.assertEqual([q["exam_number"] for q in visual], [15, 16, 40, 41, 42, 63, 64])
        import hashlib
        for q in visual:
            self.assertEqual(len(q["images"]), 1)
            im = q["images"][0]
            content = base64.b64decode(im["bytes_base64"], validate=True)
            self.assertTrue(content.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(hashlib.sha256(content).hexdigest(), im["sha256"])
            self.assertEqual(im["source_pdf_page"], q["source_pdf_page"])
            self.assertEqual(len(im["crop_rect"]), 4)
        self.assertEqual({c["text"] for q in visual if q["exam_number"] in (15, 16) for c in q["choices"]}, {""})

    def test_listening_15_16_crops_include_all_printed_option_glyphs(self):
        """Page 4 has ①/③ left of the bitmaps and ②/④ between columns."""
        _, pymupdf = ingest.dependencies()
        by_number = {q["exam_number"]: q for q in self.data["questions"]}
        with pymupdf.open(str(ingest.SOURCE_DIR / ingest.FILENAMES["listening_paper"])) as pdf:
            page = pdf[3]
            labels = []
            for block in page.get_text("rawdict")["blocks"]:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        for char in span["chars"]:
                            if char["c"] in ("①", "②", "③", "④"):
                                labels.append((char["c"], pymupdf.Rect(char["bbox"])))
            self.assertEqual(len(labels), 8)
            for number in (15, 16):
                question = by_number[number]
                crop = pymupdf.Rect(question["images"][0]["crop_rect"])
                group = [(symbol, box) for symbol, box in labels if crop.y0 <= box.y0 and box.y1 <= crop.y1]
                self.assertEqual({symbol for symbol, _ in group}, {"①", "②", "③", "④"})
                self.assertEqual(len(group), 4)
                for _, box in group:
                    # Glyph fully enclosed, with padding so rasterization does
                    # not truncate its left/top/bottom edge.
                    self.assertLess(crop.x0, box.x0 - 1)
                    self.assertGreater(crop.x1, box.x1 + 1)
                    self.assertLess(crop.y0, box.y0 - 1)
                    self.assertGreater(crop.y1, box.y1 + 1)

    def test_pdf_line_wrap_corrections_are_source_specific(self):
        qs = {q["exam_number"]: q for q in self.data["questions"]}
        groups = {g["id"]: g for g in self.data["groups"]}
        self.assertIn("근처에도", qs[22]["transcript"]["dialogue_text"])
        self.assertNotIn("근처 에도", qs[22]["transcript"]["dialogue_text"])
        for n in (25, 26):
            self.assertIn("방법들이", qs[n]["transcript"]["dialogue_text"])
            self.assertNotIn("방법 들이", qs[n]["transcript"]["dialogue_text"])
        self.assertIn("좋아했습니다", qs[48]["stem"])
        self.assertNotIn("좋아 했습니다", qs[48]["stem"])
        self.assertTrue(qs[48]["stem"].endswith("사람들도 자주 만납니다."))
        self.assertNotIn("①", qs[48]["stem"])
        # Preserve raw PDF line endings for traceability; only normalized
        # source-derived fields get the verified word join.
        self.assertIn("좋아\n했습니다", qs[48]["raw_question_text"])
        for n in (51, 52):
            passage = groups[qs[n]["group_id"]]["passage_text"]
            self.assertIn("예약하고", passage)
            self.assertNotIn("예약 하고", passage)
        for n in (61, 62):
            passage = groups[qs[n]["group_id"]]["passage_text"]
            self.assertIn("있습니다", passage)
            self.assertNotIn("있습 니다", passage)
        self.assertEqual(self.data["extraction_version"], "pdf-first-36-v4")
        self.assertEqual(ingest.STAGING.name, "staging-v4.json")

    def test_v4_staging_matches_reextracted_source_without_edits(self):
        if not ingest.STAGING.is_file():
            self.skipTest("Ignored staging-v4 is not generated on this device")
        staged = json.loads(ingest.STAGING.read_text(encoding="utf-8"))
        self.assertEqual(staged, self.data)


class FailureTests(unittest.TestCase):
    def test_verified_line_wrap_requires_exact_pdf_break_and_named_context(self):
        with self.assertRaisesRegex(ValueError, "Source line-wrap evidence changed"):
            ingest.join_verified_pdf_line_wrap(
                "호텔을 예약 하고 싶은 외국인은", location="reading_group", number=51
            )
        with self.assertRaisesRegex(ValueError, "Source line-wrap evidence changed"):
            ingest.join_verified_pdf_line_wrap(
                "호텔을 예약\n하고 싶은 외국인은; 호텔을 예약\n하고 싶은 외국인은",
                location="reading_group", number=51,
            )
        with self.assertRaises(KeyError):
            ingest.join_verified_pdf_line_wrap(
                "다른 문장의 단어를 변경하지 않습니다", location="reading_group", number=49
            )

    def test_original_manifest_hash_mismatch_fails_closed(self):
        original = json.loads(ingest.MANIFEST.read_text(encoding="utf-8"))
        mutated = copy.deepcopy(original)
        entry = next(x for x in mutated["entries"] if x.get("session") == 36 and x.get("level") == "I")
        entry["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "tampered-manifest.json"
            manifest.write_text(json.dumps(mutated), encoding="utf-8")
            with mock.patch.object(ingest, "MANIFEST", manifest):
                with self.assertRaisesRegex(ValueError, "manifest SHA-256"):
                    ingest.source_documents()

    def test_answer_duplicate_or_wrong_global_number_fails_closed(self):
        from pypdf import PdfReader
        original = PdfReader(str(ingest.SOURCE_DIR / ingest.FILENAMES["answer_key"]))
        text = original.pages[1].extract_text()
        self.assertIn("31 ② 2", text)
        fake = mock.Mock()
        fake.pages = [mock.Mock(), mock.Mock()]
        fake.pages[0].extract_text.return_value = original.pages[0].extract_text()
        fake.pages[1].extract_text.return_value = text.replace("31 ② 2", "1 ② 2", 1)
        with self.assertRaisesRegex(ValueError, "Invalid/duplicate answer row"):
            ingest.answer_table(fake)

    def test_strict_choice_order_and_count(self):
        with self.assertRaisesRegex(ValueError, "four ordered"):
            ingest.tokenize_paper(mock.Mock(pages=[mock.Mock(**{"extract_text.return_value":
                "※ [1～4]다음을 듣고 고르십시오.\n1. ① 하나 ① 둘 ③ 셋 ④ 넷\n"})]),
                "listening", "source.pdf")

    def test_existing_staging_is_idempotent_only_for_same_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "stage.json"
            self.assertEqual(ingest.write_staging({"a": 1}, path), path)
            self.assertEqual(ingest.write_staging({"a": 1}, path), path)
            with self.assertRaises(FileExistsError):
                ingest.write_staging({"a": 2}, path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1})


if __name__ == "__main__":
    unittest.main()
