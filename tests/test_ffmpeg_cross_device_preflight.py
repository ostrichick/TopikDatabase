"""Stage-8.5 pinned-FFmpeg and cross-device fingerprint regressions."""

from __future__ import annotations

import unittest

from scripts import ffmpeg_cross_device_preflight as preflight
from src import audio_35


class FFmpegCrossDevicePreflightTests(unittest.TestCase):
    def test_compare_requires_source_bounds_encoder_and_clip_sha_identity(self):
        base = {
            "schema": "topik-ffmpeg-cross-device-preflight-v1",
            "source_sha256": "a" * 64,
            "start_ms": 1,
            "end_ms": 2,
            "ffmpeg_version": "7.1",
            "ffmpeg_sha256": "b" * 64,
            "clip_sha256": "c" * 64,
        }
        self.assertEqual(preflight.compare(base, dict(base)), [])
        for field in preflight.COMPARE_FIELDS:
            changed = dict(base)
            changed[field] = "different"
            self.assertEqual(preflight.compare(base, changed), [field])

    def test_local_real_preflight_is_repeatable_with_pinned_binary(self):
        if not audio_35.DEFAULT_SOURCE.is_file() or not audio_35.PINNED_FFMPEG.is_file():
            self.skipTest("Local ignored 35-I media/pinned FFmpeg unavailable")
        first = preflight.build_report("Laptop-A")
        second = preflight.build_report("Laptop-B")
        self.assertEqual(preflight.compare(first, second), [])
        self.assertEqual(first["ffmpeg_sha256"], audio_35.PINNED_FFMPEG_SHA256)
        self.assertEqual(first["ffmpeg_version"], audio_35.PINNED_FFMPEG_VERSION)
        self.assertEqual(first["clip_sha256"], second["clip_sha256"])
        self.assertEqual(first["clip_byte_size"], second["clip_byte_size"])


if __name__ == "__main__":
    unittest.main()
