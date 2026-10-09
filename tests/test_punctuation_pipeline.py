"""36th versioned punctuation stage, checked against frozen v4 PDF extraction."""

from __future__ import annotations

import copy
import hashlib
import unittest

from src import extraction_rules, pdf_ingest_36, punctuation_pipeline as punctuation


class PunctuationPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v4 = punctuation.verified_v4(pdf_ingest_36.STAGING)

    def test_source_v4_is_frozen_and_35_uses_the_original_shared_rule(self):
        self.assertEqual(hashlib.sha256(pdf_ingest_36.STAGING.read_bytes()).hexdigest(),
                         "4c02c999b0fcbdaad48aaf9af6d0f5c754ca424f8a37885466a6704d90403684")
        self.assertEqual(extraction_rules.RULE_VERSION, "punctuation-space-v1,image-pure-v1")
        self.assertEqual(extraction_rules.PUNCTUATION_RULE_VERSION, "punctuation-space-v2")
        self.assertEqual(pdf_ingest_36.EXTRACTION_VERSION, "pdf-first-36-v4")
        self.assertEqual(pdf_ingest_36.STAGING.name, "staging-v4.json")

    def test_punctuation_rule_preserves_numbers_links_files_and_whitespace(self):
        examples = {
            "가.나,다": "가. 나, 다",
            "끝입니다.다음 문장": "끝입니다. 다음 문장",
            "바로 답입니다.(아래 확인)": "바로 답입니다. (아래 확인)",
            "3.14 1,000 12.5 1,234.50": "3.14 1,000 12.5 1,234.50",
            "https://example.com/a.b?x=1,000": "https://example.com/a.b?x=1,000",
            "name@example.com hello.world file.pdf": "name@example.com hello.world file.pdf",
            "글...다음 글..끝,": "글...다음 글..끝,",
            "글.\n다음 줄\t공백  유지": "글.\n다음 줄\t공백  유지",
        }
        for before, after in examples.items():
            with self.subTest(before=before):
                self.assertEqual(extraction_rules.normalize_punctuation_spacing(before), after)
                self.assertEqual(extraction_rules.normalize_punctuation_spacing(after), after)

    def test_v2_korean_sentence_before_numeric_term_preserves_identifiers(self):
        examples = {
            "관광 안내도 받을 수 있습니다.120전화는 24시간":
                "관광 안내도 받을 수 있습니다. 120전화는 24시간",
            "전화합니다.123번을 누르세요.": "전화합니다. 123번을 누르세요.",
            "3.14 1,000 version.2026 filename.2026 file.pdf":
                "3.14 1,000 version.2026 filename.2026 file.pdf",
            "https://example.org/있습니다.120전화 www.example.com/있습니다.2가":
                "https://example.org/있습니다.120전화 www.example.com/있습니다.2가",
            "https://example.com/자료.설명 www.example.com/자료.설명":
                "https://example.com/자료.설명 www.example.com/자료.설명",
            "https://example.com/자료.설명.pdf?검색=가.나":
                "https://example.com/자료.설명.pdf?검색=가.나",
            "사용자@도메인.한국 user@도메인.한국":
                "사용자@도메인.한국 user@도메인.한국",
            "support@있습니다.2가": "support@있습니다.2가",
            "자료.설명.pdf 보고서.최종.hwp 이름.변경.txt":
                "자료.설명.pdf 보고서.최종.hwp 이름.변경.txt",
            "C:\\자료.설명.pdf /문서/자료.설명.docx":
                "C:\\자료.설명.pdf /문서/자료.설명.docx",
            "자료.설명.pdf 문장입니다.다음 문장":
                "자료.설명.pdf 문장입니다. 다음 문장",
            "사용자@도메인.한국 바로 답입니다.그다음":
                "사용자@도메인.한국 바로 답입니다. 그다음",
            "3.14, 1,000, 12.5 ... .. \t 끝입니다.\n다음":
                "3.14, 1,000, 12.5 ... .. \t 끝입니다.\n다음",
            "반복...합니다..다음\n한 줄  두 칸":
                "반복...합니다..다음\n한 줄  두 칸",
            "안내합니다.\n120전화": "안내합니다.\n120전화",
        }
        for before, after in examples.items():
            with self.subTest(before=before):
                self.assertEqual(extraction_rules.normalize_punctuation_spacing_v2(before), after)
                self.assertEqual(extraction_rules.normalize_punctuation_spacing_v2(after), after)
        # Historical 35th behavior is pinned to the original reusable v1.
        self.assertEqual(extraction_rules.normalize_punctuation_spacing("있습니다.120전화"),
                         "있습니다.120전화")
        self.assertEqual(extraction_rules.normalize_punctuation_spacing("자료.설명.pdf"),
                         "자료. 설명.pdf")  # v1 historical behavior is unchanged.

    def test_v5_only_changes_named_display_fields_and_is_idempotent(self):
        before = copy.deepcopy(self.v4)
        v5 = punctuation.normalize_v4(before)
        self.assertEqual(before, self.v4)
        self.assertEqual(punctuation.normalize_v4(v5), v5)
        self.assertEqual(v5["extraction_version"], "pdf-first-36-v5")
        self.assertEqual(v5["punctuation_rule_version"], "punctuation-space-v2")
        self.assertEqual(punctuation.counts(v5), punctuation.EXPECTED_COUNTS)
        self.assertEqual([q["raw_question_text"] for q in before["questions"]],
                         [q["raw_question_text"] for q in v5["questions"]])
        self.assertEqual(before["sources"], v5["sources"])
        for previous, normalized in zip(before["questions"], v5["questions"]):
            self.assertEqual(previous["answer"], normalized["answer"])
            self.assertEqual(previous["images"], normalized["images"])
            self.assertEqual(previous["points"], normalized["points"])
            self.assertEqual(previous["source_relative_path"], normalized["source_relative_path"])

    def test_diff_is_complete_deterministic_and_explains_exact_changes(self):
        normalized = punctuation.normalize_v4(self.v4)
        report = punctuation.diff_report(self.v4, normalized)
        self.assertEqual(report["counts_before"], punctuation.EXPECTED_COUNTS)
        self.assertEqual(report["counts_after"], punctuation.EXPECTED_COUNTS)
        self.assertEqual(report["fields_checked"], 432)
        self.assertEqual(report["fields_changed"], 54)
        self.assertEqual(report["changes_by_type"],
                         {"choice": 8, "stem": 15, "group": 9, "transcript": 22})
        self.assertEqual(report["spaces_added"], 138)
        self.assertEqual(sum(c["spaces_added"] for c in report["changes"]), report["spaces_added"])
        self.assertTrue(all(c["spaces_added"] > 0 for c in report["changes"]))
        self.assertEqual(report, punctuation.diff_report(self.v4, normalized))
        r51 = next(g for g in normalized["groups"] if g["id"] == "036-I-R-51-52")
        self.assertIn("받을 수 있습니다. 120전화는", r51["passage_text"])
        original = next(g for g in self.v4["groups"] if g["id"] == r51["id"])
        self.assertIn("받을 수 있습니다.120전화는", original["passage_text"])

    def test_gate_rejects_unfixed_v4_and_accepts_v5(self):
        from scripts.import_exam_staging import ImportBlocked, validate

        with self.assertRaisesRegex(ImportBlocked, "supported extraction version"):
            validate(self.v4)
        self.assertEqual(validate(self.v4, allow_historical_v4=True)["status"], "validated")
        self.assertEqual(punctuation.validate_for_import(punctuation.normalize_v4(self.v4))["status"],
                         "validated")

    def test_mismatched_version_or_source_fails_closed(self):
        wrong_exam = copy.deepcopy(self.v4)
        wrong_exam["exam"]["id"] = "035-I-B"
        with self.assertRaisesRegex(ValueError, "another exam"):
            punctuation.normalize_v4(wrong_exam)
        wrong_counts = copy.deepcopy(self.v4)
        wrong_counts["questions"].pop()
        with self.assertRaisesRegex(ValueError, "source counts"):
            punctuation.normalize_v4(wrong_counts)
        changed_source = copy.deepcopy(self.v4)
        changed_source["questions"][0]["stem"] += " 임의 추가"
        with self.assertRaisesRegex(ValueError, "source SHA-256"):
            punctuation.normalize_v4(changed_source)
        unknown_version = copy.deepcopy(self.v4)
        unknown_version["extraction_version"] = "v6"
        with self.assertRaisesRegex(ValueError, "v4/v5"):
            punctuation.normalize_v4(unknown_version)

    def test_saved_v5_and_report_match_reproducible_normalization(self):
        if not pdf_ingest_36.STAGING_V5.is_file():
            self.skipTest("Ignored v5 artifact has not been produced on this machine")
        import json
        expected_v5_sha = "af72d0c340fe7b833b63dc0eb07652538e3dd292668a6e4986b4247792e9e095"
        self.assertEqual(hashlib.sha256(pdf_ingest_36.STAGING_V5.read_bytes()).hexdigest(),
                         expected_v5_sha)
        saved = json.loads(pdf_ingest_36.STAGING_V5.read_text(encoding="utf-8"))
        self.assertEqual(saved, punctuation.normalize_v4(self.v4))
        self.assertEqual(punctuation.validate_for_import(saved)["status"], "validated")
        if pdf_ingest_36.PUNCTUATION_DIFF_REPORT.is_file():
            recorded = json.loads(pdf_ingest_36.PUNCTUATION_DIFF_REPORT.read_text(encoding="utf-8"))
            self.assertEqual(recorded, punctuation.diff_report(self.v4, saved))
            self.assertEqual(recorded["output_v5_sha256"], expected_v5_sha)


if __name__ == "__main__":
    unittest.main()
