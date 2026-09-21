"""Non-mutating 35th-audio regression checks; output tests use temporary dirs."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import audio_35


def synthetic_long_pauses():
    """31 response gaps, including three short follow-up question utterances."""
    pauses = []
    position = 70000
    for index in range(31):
        duration = 12000 if index == 0 else (39000 if index >= 25 else 20000)
        pauses.append((position, position + duration))
        if index < 30:
            next_question = index + 1
            speech = 1200 if next_question in (26, 28, 30) else 75000
            position += duration + speech
    return pauses


class Audio35Tests(unittest.TestCase):
    def test_31_pauses_form_27_unverified_candidates_and_shared_groups(self):
        pauses = synthetic_long_pauses()
        data = audio_35._propose(pauses, pauses[-1][1] + 5000, "a" * 64)
        self.assertEqual(len(data), 27)
        self.assertEqual([r["questions"] for r in data[-3:]],
                         [[25, 26], [27, 28], [29, 30]])
        self.assertEqual([q for row in data for q in row["questions"]], list(range(1, 31)))
        self.assertTrue(all(row["status"] == "candidate_unverified" and
                            row["requires_human_audio_review"] and
                            row["source_sha256"] == "a" * 64 for row in data))
        self.assertTrue(all(row["contains_response_pause"] for row in data[-3:]))

    def test_uncertain_audio_never_gets_invented_question_mapping(self):
        pauses = synthetic_long_pauses()
        self.assertEqual(audio_35._propose(pauses[:-1], pauses[-1][1] + 5000, "a" * 64), [])
        bad = list(pauses)
        bad[26] = (bad[25][1] - 20, bad[25][1] + 39000)
        self.assertEqual(audio_35._propose(bad, bad[-1][1] + 5000, "a" * 64), [])
        broken_followup = list(pauses)
        broken_followup[26] = (broken_followup[25][0] + 150000,
                              broken_followup[25][0] + 189000)
        self.assertEqual(audio_35._propose(broken_followup,
                                           broken_followup[-1][1] + 5000, "a" * 64), [])

    def test_source_header_rejects_non_mp3(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fake.mp3"
            path.write_bytes(b"<html>not an audio file")
            with self.assertRaises(audio_35.AudioError):
                audio_35.probe_audio(path)

    def test_export_refuses_original_corpus_existing_and_wrong_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "audio.mp3"
            source.write_bytes(b"\xff\xfb" + b"\0" * 1000)
            output = root / "output.mp3"
            output.write_bytes(b"keep")
            with self.assertRaisesRegex(audio_35.AudioError, "overwrite"):
                audio_35.export_segment(source, output, 0, 1000)
            output.unlink()
            with patch.object(audio_35, "probe_audio", return_value=10000):
                with self.assertRaisesRegex(audio_35.AudioError, "changed"):
                    audio_35.export_segment(source, output, 0, 1000,
                                            expected_source_sha256="0" * 64)
                with self.assertRaisesRegex(audio_35.AudioError, "boundaries"):
                    audio_35.export_segment(source, output, -1, 1000)
                with self.assertRaisesRegex(audio_35.AudioError, "boundaries"):
                    audio_35.export_segment(source, output, 9000, 10001)

    def test_export_uses_precise_decode_and_no_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "audio.mp3"
            source.write_bytes(b"\xff\xfb" + b"\0" * 1000)
            destination = root / "segment.mp3"
            recorded = []

            def fake_run(command):
                recorded.append(command)
                Path(command[-1]).write_bytes(b"\xff\xfb" + b"test" * 64)
                return type("Result", (), {"returncode": 0})()

            with patch.object(audio_35, "probe_audio", return_value=10000), \
                    patch.object(audio_35, "_ffmpeg", return_value="ffmpeg"), \
                    patch.object(audio_35, "_run", side_effect=fake_run):
                self.assertEqual(audio_35.export_segment(source, destination, 1200, 3200,
                                                         expected_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
                                 destination.resolve())
            self.assertTrue(destination.exists())
            self.assertEqual(recorded[0][recorded[0].index("-ss") + 1], "1.200")
            self.assertEqual(recorded[0][recorded[0].index("-t") + 1], "2.000")
            self.assertEqual(recorded[0][recorded[0].index("-c:a") + 1], "libmp3lame")
            self.assertFalse(list(root.glob("*.part")))

    def test_real_source_has_stable_pause_pattern_without_database_mutation(self):
        if not audio_35.DEFAULT_SOURCE.is_file():
            self.skipTest("Local ignored corpus not available")
        report = audio_35.analyze_audio()
        self.assertEqual(report["duration_ms"], 2356715)
        self.assertTrue(report["thresholds_stable"])
        self.assertEqual(report["long_pause_counts"], {"-30": 31, "-35": 31,
                                                        "-40": 31, "-45": 31})
        self.assertEqual(report["candidate_count"], 27)
        self.assertEqual([r["questions"] for r in report["candidates"][-3:]],
                         [[25, 26], [27, 28], [29, 30]])


if __name__ == "__main__":
    unittest.main()
