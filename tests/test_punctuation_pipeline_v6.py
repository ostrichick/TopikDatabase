"""Immutable v5 to v6 spacing extension and non-text provenance guards."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest

from src import pdf_ingest_36, punctuation_pipeline_v6 as v6
from src.extraction_rules import (
    RULE_VERSION, PUNCTUATION_RULE_VERSION, PUNCTUATION_RULE_VERSION_V3,
    normalize_punctuation_spacing, normalize_punctuation_spacing_v2,
    normalize_punctuation_spacing_v3,
)
from src.punctuation_pipeline import encoded_staging


class PunctuationPipelineV6Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v5 = v6.verified_v5(pdf_ingest_36.STAGING_V5)

    def test_historical_35_and_v5_normalizers_are_unchanged(self):
        self.assertEqual(RULE_VERSION, "punctuation-space-v1,image-pure-v1")
        self.assertEqual(PUNCTUATION_RULE_VERSION, "punctuation-space-v2")
        self.assertEqual(PUNCTUATION_RULE_VERSION_V3, "punctuation-space-v3")
        self.assertEqual(normalize_punctuation_spacing("그렇습니까?그럼"), "그렇습니까?그럼")
        self.assertEqual(normalize_punctuation_spacing_v2("그렇습니까?그럼"), "그렇습니까?그럼")

    def test_new_question_exclamation_and_speaker_colon_spacing(self):
        cases = {
            "그렇습니까?그럼, 다시요!네": "그렇습니까? 그럼, 다시요! 네",
            "아!우리 친구": "아! 우리 친구",
            "어디입니까?<보기>와 같이": "어디입니까? <보기>와 같이",
            "여자 :공책이에요?\n\n남자 :_________":
                "여자 : 공책이에요?\n\n남자 : _________",
            "남자 :(놀란 듯이)그렇습니까?그래요":
                "남자 : (놀란 듯이)그렇습니까? 그래요",
            "끝입니다.120전화는 24시간": "끝입니다. 120전화는 24시간",
            "어디죠?123번에 있어요": "어디죠? 123번에 있어요",
            "읽으세요:3번을 보세요": "읽으세요: 3번을 보세요",
            "그렇습니까?\n다음 줄\t또": "그렇습니까?\n다음 줄\t또",
            "계속?!그렇죠": "계속?! 그렇죠",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                actual = normalize_punctuation_spacing_v3(text)
                self.assertEqual(actual, expected)
                self.assertEqual(normalize_punctuation_spacing_v3(actual), expected)

    def test_preserves_identifiers_urls_times_files_decimal_whitespace(self):
        cases = [
            "12:30 3.14 1,000 2026-10-09T18:30:00 1:2:3",
            "https://example.org/?q=그렇습니까?그래요&a=예:아니오",
            "www.example.com/여자:남자",
            "user@example.com 사용자@도메인.한국",
            "자료.설명.pdf 파일:정답.txt image.png",
            "C:\\자료.설명.pdf /문서/자료.설명.docx",
            "a?! b!  c:\\work  12:30\n\t",
            "문장...다음 글..끝...  !?  : :",
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(normalize_punctuation_spacing_v3(value), value)
        with self.assertRaises(TypeError):
            normalize_punctuation_spacing_v3(None)

    def test_snapshot_is_exact_versioned_only_whitespace_additions(self):
        v5 = copy.deepcopy(self.v5)
        v6_data = v6.normalize_v5(v5)
        self.assertEqual(v5, self.v5)
        self.assertEqual(v6.normalize_v5(v6_data), v6_data)
        self.assertEqual(v6_data["extraction_version"], "pdf-first-36-v6")
        self.assertEqual(v6_data["punctuation_rule_version"], PUNCTUATION_RULE_VERSION_V3)
        report = v6.diff_report(v5, v6_data)
        self.assertEqual(report["fields_checked"], 432)
        self.assertEqual(report["fields_changed"], 33)
        self.assertEqual(report["changes_by_type"], {"group": 3, "transcript": 30})
        self.assertEqual(report["spaces_added"], 109)
        for change in report["changes"]:
            self.assertEqual(change["before"].replace(" ", ""), change["after"].replace(" ", ""))
        self.assertEqual(v6_data["sources"], v5["sources"])
        self.assertEqual([x["raw_question_text"] for x in v6_data["questions"]],
                         [x["raw_question_text"] for x in v5["questions"]])
        self.assertEqual([x["answer"] for x in v6_data["questions"]],
                         [x["answer"] for x in v5["questions"]])
        self.assertEqual(v6.validate_for_import(v6_data)["status"], "validated")

    def test_frozen_v5_refuses_mutation_and_wrong_versions(self):
        altered = copy.deepcopy(self.v5)
        altered["questions"][0]["stem"] += "무단 변경"
        with self.assertRaisesRegex(ValueError, "frozen original v5"):
            v6.normalize_v5(altered)
        altered = copy.deepcopy(self.v5)
        altered["extraction_version"] = "random"
        with self.assertRaisesRegex(ValueError, "versioned v5/v6"):
            v6.normalize_v5(altered)

    def test_derived_artifacts_are_reproducible(self):
        if not pdf_ingest_36.STAGING_V6.is_file():
            self.skipTest("v6 artifact not generated on this machine")
        source = v6.normalize_v5(self.v5)
        self.assertEqual(pdf_ingest_36.STAGING_V6.read_bytes(), encoded_staging(source))
        diff = json.loads(pdf_ingest_36.PUNCTUATION_DIFF_V6_REPORT.read_text(encoding="utf-8"))
        self.assertEqual(diff, v6.diff_report(self.v5, source))
        self.assertEqual(diff["output_v6_sha256"],
                         hashlib.sha256(pdf_ingest_36.STAGING_V6.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
