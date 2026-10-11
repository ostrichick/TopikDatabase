"""36th v7 candidate never mutates previous independently audited sources."""

import copy
import hashlib
import json
import unittest
from pathlib import Path

from src import transcript_speaker_pipeline as s
from src.punctuation_pipeline import encoded_staging


ROOT = Path(__file__).resolve().parents[1]
V6 = ROOT / "topik-past-papers/derived/036-I-B/staging-v6.json"


class SourceFrozenSpeakerPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not V6.is_file():
            raise unittest.SkipTest("Local immutable v6 staging unavailable")
        cls.source = json.loads(V6.read_text(encoding="utf-8"))

    def test_v7_changes_only_30_transcripts_and_89_spoken_turns(self):
        before = copy.deepcopy(self.source)
        result, diff = s.normalize_v6(self.source)
        self.assertEqual(self.source, before)
        self.assertEqual(diff["changed_transcripts"], 30)
        self.assertEqual(diff["total_speaker_turns"], 89)
        self.assertEqual(result["extraction_version"], "pdf-first-36-v7")
        self.assertEqual(result["speaker_turn_rule_version"], "speaker-colon-v1")
        self.assertEqual(len(result["questions"]), 70)
        self.assertEqual(len(result["groups"]), 26)
        self.assertEqual(len(result["sources"]), 6)
        self.assertEqual(len({q["id"] for q in result["questions"]}), 70)
        self.assertEqual(hashlib.sha256(encoded_staging(before)).hexdigest(), s.FROZEN_V6_SHA256)
        for row in diff["changes"]:
            self.assertNotEqual(row["before"], row["after"])
            self.assertIn("남자: " if "남자" in row["after"] else "여자: ", row["after"])
            self.assertNotIn("남자 :", row["after"])
            self.assertNotIn("여자 :", row["after"])
        outputs = {q["exam_number"]: q["transcript"]["dialogue_text"]
                   for q in result["questions"] if q.get("transcript")}
        for a, b in ((25, 26), (27, 28), (29, 30)):
            self.assertEqual(outputs[a], outputs[b])

    def test_frozen_checksum_mismatch_rejected(self):
        tampered = copy.deepcopy(self.source)
        tampered["questions"][0]["transcript"]["dialogue_text"] += "추가"
        with self.assertRaisesRegex(ValueError, "checksum"):
            s.normalize_v6(tampered)


if __name__ == "__main__":
    unittest.main()
