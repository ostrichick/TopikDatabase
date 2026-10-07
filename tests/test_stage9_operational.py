"""Stage 9 operational helper regressions; no live operational DB mutation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import stage9_operational, stage9_smoke


class Stage9OperationalTests(unittest.TestCase):
    def test_immutable_source_and_media_match_approved_cutover_baseline(self):
        result = stage9_operational.verify_immutable_source()
        self.assertEqual(result["sqlite_sha256"], stage9_operational.EXPECTED_SQLITE_SHA256)
        self.assertEqual(result["source_media_verified"], 5)

    def test_reset_refuses_unapproved_database_before_credentials_are_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "unapproved Stage 9 database"):
                stage9_operational.reset_database(
                    Path(temporary), port=55432, database="postgres"
                )

    def test_mutating_smoke_requires_explicit_postgresql_url(self):
        with self.assertRaisesRegex(RuntimeError, "explicit PostgreSQL URL"):
            stage9_smoke.run_smoke("C:/not-a-database.sqlite")


if __name__ == "__main__":
    unittest.main()
