"""Regression checks against the source 35th TOPIK I transcript.

Extraction uses the original PDF in read-only mode. No test edits a DB or PDF.
"""

import hashlib
import re
import sys
import unittest
from pathlib import Path

from src import pilot_35
from src.transcript_35 import extract_transcripts


SOURCE = Path(__file__).resolve().parents[1] / "topik-past-papers" / "35th" / "35th-TOPIK-I-Listening-Transcript.pdf"


class TestTranscript35(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SOURCE.is_file():
            raise unittest.SkipTest("Private TOPIK source corpus is not available in this checkout")
        cls.original_pdf_hash = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
        cls.baseline = extract_transcripts(SOURCE, restore_visual_spacing=False)
        cls.questions = extract_transcripts(SOURCE)

    def test_all_questions_and_expected_pdf_pages(self):
        self.assertEqual(list(self.questions), list(range(1, 31)))
        self.assertEqual(self.questions[1]["pdf_page"], 1)
        self.assertEqual(self.questions[4]["pdf_page"], 2)
        self.assertEqual(self.questions[15]["pdf_page"], 5)
        self.assertEqual(self.questions[25]["pdf_page"], 10)
        self.assertEqual(self.questions[29]["pdf_page"], 12)

    def test_only_dialogue_no_choices_or_adjacent_example(self):
        first = self.questions[1]["text"]
        self.assertIn("우산", first)
        self.assertNotIn("①", first)
        self.assertNotIn("※", first)
        self.assertNotIn("공부", first)  # prior sample, not question 1
        self.assertIn("꽃", self.questions[15]["text"])
        self.assertNotIn("①", self.questions[15]["text"])
        self.assertNotIn("맛있게", self.questions[4]["text"])  # following sample
        for question in self.questions.values():
            self.assertNotIn("①", question["text"])
            self.assertNotIn("※", question["text"])
            self.assertNotIn("문항번호", question["text"])

    def test_shared_passages_and_review(self):
        for left, right, keyword in [(25, 26, "호텔"), (27, 28, "소포"), (29, 30, "김치")]:
            self.assertEqual(self.questions[left]["text"], self.questions[right]["text"])
            self.assertIn(keyword, self.questions[left]["text"])
            self.assertEqual(self.questions[left]["pdf_page"], self.questions[right]["pdf_page"])
            self.assertTrue(any("shared" in warning.lower() for warning in self.questions[right]["warnings"]))
        for question in self.questions.values():
            self.assertEqual(question["review_status"], "needs_manual_review")
            self.assertTrue(question["text"].strip())

    def test_wrong_document_is_rejected(self):
        wrong = SOURCE.parent / "35th-TOPIK-I-Papers.pdf"
        with self.assertRaises(ValueError):
            extract_transcripts(wrong)

    def test_glyph_geometry_proves_restored_word_boundaries(self):
        """Source glyph advances distinguish word spaces from adjacent glyphs.

        These are measured on the raw PDF, not inferred from Korean vocabulary.
        A visible word gap is about 1.38-1.44x font size, ordinary adjacent
        Hangul glyphs about 0.92x; extractor threshold is 1.18x.
        """
        sys.path.insert(0, str(SOURCE.parents[2] / ".verification_deps"))
        import pymupdf

        def glyph_advance(page, pair):
            for block in page.get_text("rawdict")["blocks"]:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        chars = span["chars"]
                        index = "".join(char["c"] for char in chars).find(pair)
                        if index != -1:
                            return ((chars[index + 1]["origin"][0] - chars[index]["origin"][0])
                                    / span["size"])
            self.fail(f"Original PDF has no adjacent glyph pair {pair!r}")

        # Each positive pair is a real, unspaced char pair in baseline.
        evidence = {
            1: (("이있", "산이"),),  # 우산이 있어요 vs. 우산이
            2: (("수씨", "민수"), ("저먼", "먼저")),  # 민수 씨, 저 먼저
            4: (("한개", "개에"), ("에천", "개에"), ("천원", "개에")),
            10: (("에도", "호텔"),),  # 호텔에 도착했습니다 (shared Q25/26)
        }
        with pymupdf.open(str(SOURCE)) as pdf:
            for pdf_page, examples in evidence.items():
                for separated, joined in examples:
                    with self.subTest(pdf_page=pdf_page, separated=separated):
                        self.assertGreater(glyph_advance(pdf[pdf_page - 1], separated), 1.18)
                        self.assertLess(glyph_advance(pdf[pdf_page - 1], joined), 1.18)

    def test_restoration_exact_source_examples_and_all_content_identity(self):
        phrases = {
            1: "우산이 있어요",
            5: "민수 씨, 저 먼저 갈게요",
            11: "한 개에 천 원",
            25: "호텔에 도착했습니다",
            26: "호텔에 도착했습니다",
        }
        self.assertEqual(set(self.baseline), set(range(1, 31)))
        self.assertEqual(set(self.questions), set(range(1, 31)))
        for number in range(1, 31):
            with self.subTest(question=number):
                old, new = self.baseline[number], self.questions[number]
                self.assertTrue(old["text"].strip())
                self.assertTrue(new["text"].strip())
                self.assertEqual(old["pdf_page"], new["pdf_page"])
                self.assertEqual(re.sub(r"\s+", "", old["text"]),
                                 re.sub(r"\s+", "", new["text"]))
                self.assertEqual(new["review_status"], "needs_manual_review")
                self.assertTrue(any("human" in warning for warning in new["warnings"]))
        for number, phrase in phrases.items():
            self.assertIn(phrase, self.questions[number]["text"])
        for first, second in ((25, 26), (27, 28), (29, 30)):
            self.assertEqual(self.baseline[first]["text"], self.baseline[second]["text"])
            self.assertEqual(self.questions[first]["text"], self.questions[second]["text"])

    def test_extraction_never_modifies_source_or_answers(self):
        answer_pdf = SOURCE.parent / "35th-TOPIK-I-Answer-Sheet.pdf"
        preview = next(SOURCE.parent.glob("*.html"))
        answer_before = hashlib.sha256(answer_pdf.read_bytes()).hexdigest()
        preview_before = hashlib.sha256(preview.read_bytes()).hexdigest()
        expected = pilot_35.pdf_answer_table(answer_pdf)
        source_questions, _, _ = pilot_35.load_preview(preview)
        before = {q["number"]: q["answer"] for q in source_questions if q["section"] == "listening"}
        self.assertEqual(len(before), 30)
        self.assertEqual({num: record[0] for num, record in expected["listening"].items()}, before)
        extract_transcripts(SOURCE, restore_visual_spacing=False)
        extract_transcripts(SOURCE, restore_visual_spacing=True)
        self.assertEqual(hashlib.sha256(SOURCE.read_bytes()).hexdigest(), self.original_pdf_hash)
        self.assertEqual(hashlib.sha256(answer_pdf.read_bytes()).hexdigest(), answer_before)
        self.assertEqual(hashlib.sha256(preview.read_bytes()).hexdigest(), preview_before)
        self.assertEqual({num: record[0] for num, record in pilot_35.pdf_answer_table(answer_pdf)["listening"].items()},
                         before)


if __name__ == "__main__":
    unittest.main()
