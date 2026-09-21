"""Regression checks for reproducible corrections of the 35th TOPIK I pilot.

The preview, source corpus and existing derived database are read-only.  These
tests decode the historical PNGs in memory; they never rewrite source assets.
"""

from __future__ import annotations

import base64
import hashlib
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src import extraction_rules, pilot_35
from src.transcript_35 import extract_transcripts


ROOT = Path(__file__).resolve().parents[1]
PREVIEW = pilot_35.SESSION_DIR / "TOPIK_35_I_문항_미리보기.html"
TRANSCRIPT = pilot_35.SESSION_DIR / "35th-TOPIK-I-Listening-Transcript.pdf"
DEPS = ROOT / "topik-past-papers" / ".verification_deps"

# Independent expected rectangles, not looked up from the correction module.
# Coordinate order: x0, y0, x1, y1; None denotes the already-clean shared art.
KNOWN_IMAGES = {
    "assets/035-I-L-15-stimulus.png": (
        "2cede69c53ecafe4345c72f838c957ef7f328bf5f5d4721810b424b781fb472a",
        (934, 568), (76, 0, 888, 486)),
    "assets/035-I-L-16-stimulus.png": (
        "30a3b3c2047d83cd7f60ce56032173bf691c9060f4ad11b380d25af85f45bf55",
        (934, 652), (76, 0, 888, 478)),
    "assets/035-I-R-40-stimulus.png": (
        "d5c96e81c5a5751cd4684f1052280ae78ded3ac27b95ca2eb69cf3ef1c28b329",
        (900, 394), (184, 47, 756, 389)),
    "assets/035-I-R-41-stimulus.png": (
        "f43b0d13afb8df5017104443284e1c0a5823ec3e3f998a5f9b5c7bbfa3b74e89",
        (902, 450), (246, 40, 690, 442)),
    "assets/035-I-R-42-stimulus.png": (
        "02d9bd2abf95f5f73d157ab0a6ca8e0c468679b005f839ac0a12e3ab8f1d5126",
        (902, 426), (59, 33, 878, 423)),
    "assets/035-I-R-63-64-stimulus.png": (
        "60f62999ae629a8d1d5c9c43d015ade25b50a50afc54789ba229895c133f94e8",
        (900, 452), None),
}


def original_png(uri: str) -> bytes:
    """Decode only the preview's original PNG bytes without executing HTML."""
    prefix, separator, encoded = uri.partition(",")
    if not separator or prefix != "data:image/png;base64":
        raise AssertionError("Unexpected preview image representation")
    return base64.b64decode(encoded, validate=True)


def rgb_crop(samples: bytes, width: int, box: tuple[int, int, int, int]) -> bytes:
    x0, y0, x1, y1 = box
    return b"".join(samples[(y * width + x0) * 3:(y * width + x1) * 3]
                    for y in range(y0, y1))


def dark_pixels(samples: bytes, width: int, box: tuple[int, int, int, int]) -> int:
    """Count dark pixels in an original label location; do not infer OCR text."""
    x0, y0, x1, y1 = box
    return sum(sum(samples[(y * width + x) * 3:(y * width + x) * 3 + 3]) < 600
               for y in range(y0, y1) for x in range(x0, x1))


class TestExtractionRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PREVIEW.is_file() or not TRANSCRIPT.is_file():
            raise unittest.SkipTest("Private 35th TOPIK I preview/transcript not installed")
        sys.path.insert(0, str(DEPS))
        import pymupdf
        cls.pymupdf = pymupdf
        cls.questions, cls.groups, cls.images = pilot_35.load_preview(PREVIEW)
        # Historical punctuation counts describe the pre-spacing extractor,
        # not the new glyph-position restoration (which already formats text).
        cls.transcripts = extract_transcripts(TRANSCRIPT, restore_visual_spacing=False)
        cls.restored_transcripts = extract_transcripts(TRANSCRIPT, restore_visual_spacing=True)

    def test_punctuation_fixtures_are_conservative_and_idempotent(self):
        fixtures = {
            "저는 학생입니다.친구입니다.": "저는 학생입니다. 친구입니다.",
            "네,우산이에요. 아니요,회사에 없어요.": "네, 우산이에요. 아니요, 회사에 없어요.",
            "문장입니다.( ㉠ ) 다음 문장": "문장입니다. ( ㉠ ) 다음 문장",
            "답입니다.  그다음 문장": "답입니다.  그다음 문장",
            "끝입니다.\n다음 줄": "끝입니다.\n다음 줄",
            "한 문장입니다.": "한 문장입니다.",
            "3.14, 1,000, 12.5%": "3.14, 1,000, 12.5%",
            "https://example.com/a.b?x=1,000": "https://example.com/a.b?x=1,000",
            "a.b@example.com, support@example.org": "a.b@example.com, support@example.org",
            "version 1.2.3; file.pdf; U.S.A.": "version 1.2.3; file.pdf; U.S.A.",
            "잠깐...그러나": "잠깐...그러나",
            "쉼표,,마침표..다음": "쉼표,,마침표..다음",
            "빈칸( )입니다.그다음": "빈칸( )입니다. 그다음",
            "": "",
            "\n  ": "\n  ",
        }
        for original, expected in fixtures.items():
            with self.subTest(original=original):
                actual = extraction_rules.normalize_punctuation_spacing(original)
                self.assertEqual(actual, expected)
                self.assertEqual(extraction_rules.normalize_punctuation_spacing(actual), actual)
        with self.assertRaises(TypeError):
            extraction_rules.normalize_punctuation_spacing(None)
        self.assertTrue(extraction_rules.RULE_VERSION)

    def test_historical_preview_and_transcript_have_exactly_128_fixes(self):
        # Raw question text is archival provenance and intentionally excluded:
        # its extra 31 matches duplicate the display stem/choice contents.
        fields = {
            "stems": ([item["stem"] for item in self.questions], 19),
            "choices": ([text for item in self.questions for text in item["options"].values()], 12),
            "instructions": ([item["instruction"] for item in self.groups], 0),
            "passages": ([item["passage_text"] for item in self.groups], 33),
            "transcripts": ([item["text"] for item in self.transcripts.values()], 64),
        }
        observed = 0
        for name, (values, expected_count) in fields.items():
            with self.subTest(field=name):
                before = sum(len(re.findall(r"(?<=[가-힣)\]])[.,](?=[가-힣(])", value))
                             for value in values)
                corrected = [extraction_rules.normalize_punctuation_spacing(value) for value in values]
                self.assertEqual(before, expected_count)
                self.assertEqual(sum(len(new) - len(old) for old, new in zip(values, corrected)), expected_count)
                self.assertEqual([extraction_rules.normalize_punctuation_spacing(new) for new in corrected], corrected)
                observed += expected_count
        self.assertEqual(observed, 128)

    def test_exact_image_geometry_pixels_and_source_hashes(self):
        self.assertEqual(set(self.images), set(KNOWN_IMAGES))
        self.assertEqual(extraction_rules.image_keys(), set(KNOWN_IMAGES))
        source_preview_before = hashlib.sha256(PREVIEW.read_bytes()).hexdigest()
        self.assertEqual(len({extraction_rules.derived_image_key(key) for key in self.images}), 6)
        for key, (sha256, dimensions, box) in KNOWN_IMAGES.items():
            with self.subTest(key=key):
                original = original_png(self.images[key])
                self.assertEqual(hashlib.sha256(original).hexdigest(), sha256)
                src = self.pymupdf.Pixmap(original)
                self.assertEqual((src.width, src.height, src.n, src.alpha), (*dimensions, 3, 0))
                corrected = extraction_rules.clean_question_image(key, original)
                self.assertTrue(corrected.startswith(b"\x89PNG\r\n\x1a\n"))
                if box is None:
                    self.assertIs(corrected, original)
                    self.assertEqual(extraction_rules.derived_image_key(key), key)
                else:
                    x0, y0, x1, y1 = box
                    dst = self.pymupdf.Pixmap(corrected)
                    self.assertEqual((dst.width, dst.height, dst.n, dst.alpha),
                                     (x1 - x0, y1 - y0, 3, 0))
                    self.assertEqual(dst.samples, rgb_crop(src.samples, src.width, box))
                    self.assertNotEqual(extraction_rules.derived_image_key(key), key)
                    self.assertTrue(extraction_rules.derived_image_key(key).endswith("-clean-v1.png"))
                self.assertEqual(hashlib.sha256(original).hexdigest(), sha256)
        self.assertEqual(hashlib.sha256(PREVIEW.read_bytes()).hexdigest(), source_preview_before)

    def test_listening_choice_labels_stay_in_cropped_geometry(self):
        # The original four option labels occupy upper-left/upper-right and
        # lower-left/lower-right blocks.  Confirm these ink anchors survive
        # unchanged in the crop.  This is a geometry test, not image OCR.
        for question in (15, 16):
            key = f"assets/035-I-L-{question}-stimulus.png"
            original = self.pymupdf.Pixmap(original_png(self.images[key]))
            cleaned = self.pymupdf.Pixmap(
                extraction_rules.clean_question_image(key, original_png(self.images[key])))
            boxes = ((76, 0, 155, 42), (490, 0, 575, 42),
                     (76, 235, 155, 275), (490, 235, 575, 275))
            for option_number, box in enumerate(boxes, 1):
                with self.subTest(question=question, option=option_number):
                    self.assertGreater(dark_pixels(original.samples, original.width, box), 100)
                    translated = (box[0] - 76, box[1], box[2] - 76, box[3])
                    self.assertEqual(rgb_crop(original.samples, original.width, box),
                                     rgb_crop(cleaned.samples, cleaned.width, translated))

    def test_tampering_and_unknown_image_sources_fail_closed(self):
        key = "assets/035-I-L-15-stimulus.png"
        original = original_png(self.images[key])
        altered = original[:-1] + bytes([original[-1] ^ 1])
        with self.assertRaisesRegex(ValueError, "Source image changed"):
            extraction_rules.clean_question_image(key, altered)
        with self.assertRaises(ValueError):
            extraction_rules.clean_question_image("../other.png", original)
        with self.assertRaises(ValueError):
            extraction_rules.derived_image_key("../other.png")

    def test_fresh_import_corrects_active_assets_but_retains_six_originals(self):
        original_db = pilot_35.CORPUS / "derived" / "035-I-B.sqlite"
        originals = [PREVIEW, TRANSCRIPT,
                     pilot_35.SESSION_DIR / "35th-TOPIK-I-Papers.pdf",
                     pilot_35.SESSION_DIR / "35th-TOPIK-I-Answer-Sheet.pdf",
                     pilot_35.SESSION_DIR / "35-TOPIK-I-Listening-Audio-File.mp3"]
        if original_db.is_file():
            originals.append(original_db)
        source_digests = {path: pilot_35.digest(path) for path in originals}
        expected_questions = {item["id"]: item for item in self.questions}
        expected_groups = {item["group_id"]: item for item in self.groups}
        with tempfile.TemporaryDirectory(prefix="topik-corrections-test-") as tmp:
            directory = Path(tmp)
            db_path, report_path = directory / "fresh.sqlite", directory / "report.json"
            with patch.object(pilot_35, "OUTPUT_DIR", directory), \
                 patch.object(pilot_35, "DB_PATH", db_path), \
                 patch.object(pilot_35, "REPORT_PATH", report_path):
                report = pilot_35.main()
            self.assertEqual(report["image_blobs"], 11)
            self.assertEqual(report["active_image_blobs"], 6)
            self.assertEqual(report["image_question_links"], 7)
            self.assertEqual(report["extraction_correction_version"], extraction_rules.RULE_VERSION)

            with closing(sqlite3.connect(db_path)) as connection:
                self.assertFalse(connection.execute("PRAGMA foreign_key_check").fetchall())
                blobs = dict(connection.execute("SELECT key,bytes FROM images"))
                self.assertEqual(set(blobs),
                                 set(KNOWN_IMAGES) |
                                 {extraction_rules.derived_image_key(key) for key in KNOWN_IMAGES})
                for key, uri in self.images.items():
                    with self.subTest(original=key):
                        source = original_png(uri)
                        self.assertEqual(blobs[key], source)
                        self.assertEqual(blobs[extraction_rules.derived_image_key(key)],
                                         extraction_rules.clean_question_image(key, source))

                active = dict(connection.execute("SELECT q.exam_number,qi.image_key "
                                                 "FROM question_images qi JOIN questions q "
                                                 "ON q.id=qi.question_id"))
                self.assertEqual(set(active), {15, 16, 40, 41, 42, 63, 64})
                for number, active_key in active.items():
                    historical = next(q for q in self.questions if q["number"] == number)["image_paths"][0]
                    self.assertEqual(active_key, extraction_rules.derived_image_key(historical))
                self.assertEqual(len(set(active.values())), 6)

                for qid, stem, raw in connection.execute("SELECT id,stem,raw_question_text FROM questions"):
                    prior = expected_questions[qid]
                    self.assertEqual(stem, extraction_rules.normalize_punctuation_spacing(prior["stem"]))
                    self.assertEqual(raw, prior["raw_question_text"])  # Preserve archival provenance.
                for qid, number, text in connection.execute("SELECT question_id,number,text FROM choices"):
                    prior = expected_questions[qid]["options"][str(number)]
                    self.assertEqual(text, extraction_rules.normalize_punctuation_spacing(prior))
                for group_id, instruction, passage in connection.execute(
                        "SELECT id,instruction,passage_text FROM question_groups"):
                    prior = expected_groups[group_id]
                    self.assertEqual(instruction, extraction_rules.normalize_punctuation_spacing(prior["instruction"]))
                    self.assertEqual(passage, extraction_rules.normalize_punctuation_spacing(prior["passage_text"]))
                for number, dialogue in connection.execute(
                        "SELECT q.exam_number,t.dialogue_text FROM transcripts t "
                        "JOIN questions q ON q.id=t.question_id"):
                    self.assertEqual(dialogue, self.restored_transcripts[number]["text"])

        self.assertEqual({path: pilot_35.digest(path) for path in originals}, source_digests)


if __name__ == "__main__":
    unittest.main()
