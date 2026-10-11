"""TOPIK listening speaker punctuation is separate from general punctuation."""

import copy
import unittest

from src.extraction_rules import SPEAKER_TURN_RULE_VERSION, normalize_speaker_turn_spacing
from src.transcript_35 import _format_dialogue
from src.review_ui import ReviewStore


class SpeakerTurnFormattingTests(unittest.TestCase):
    def test_illustrative_dialogue_and_idempotency(self):
        examples = (
            ("가 :할말\n나:  할말", "가: 할말\n나: 할말"),
            ("철수  :대사1\n영희 :   대사2", "철수: 대사1\n영희: 대사2"),
            ("남자 :여보세요.\n\n여자 : ______", "남자: 여보세요.\n\n여자: ______"),
            ("  선생님 : 안녕하세요.\n학생:네!", "  선생님: 안녕하세요.\n학생: 네!"),
            ("남자:      (작게) 안녕하세요.", "남자: (작게) 안녕하세요."),
            ("여자:안녕하세요.\r\n남자 :네.", "여자: 안녕하세요.\r\n남자: 네."),
        )
        self.assertEqual(SPEAKER_TURN_RULE_VERSION, "speaker-colon-v1")
        for prior, expected in examples:
            with self.subTest(prior=prior):
                self.assertEqual(normalize_speaker_turn_spacing(prior), expected)
                self.assertEqual(normalize_speaker_turn_spacing(expected), expected)

    def test_protect_internal_colons_metadata_and_non_turn_text(self):
        for raw in (
            "지금은 12:30입니다.",
            "URL은 https://example.org/a:b입니다.",
            "시간: 12:30\n정답 : ③",
            "대화 중간에 여자 :라고 써도 대사 아님",
            "남자:\n계속 읽으세요.",
            "학생: \n뒤에 음성이 없습니다.",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_speaker_turn_spacing(raw), raw)
        with self.assertRaises(TypeError):
            normalize_speaker_turn_spacing(None)

    def test_35_extraction_formatter_reuses_shared_rule(self):
        self.assertEqual(_format_dialogue("여자 :안녕하세요?철수:질문"),
                         "여자: 안녕하세요? 철수:질문")

    def test_review_preview_is_derived_without_mutating_original_db_field(self):
        detail = {
            "exam_id": "035-I-B", "stem": "", "choices": [],
            "group": {"instruction": "", "passage_text": ""},
            "transcript": {"text": "남자 : 안녕.\n여자:반가워."},
        }
        before = copy.deepcopy(detail)
        result = ReviewStore._add_punctuation_preview(detail)
        self.assertEqual(result["transcript"]["display_text"], "남자: 안녕.\n여자: 반가워.")
        self.assertEqual(result["transcript"]["text"], before["transcript"]["text"])


if __name__ == "__main__":
    unittest.main()
