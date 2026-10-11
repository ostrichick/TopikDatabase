"""Fail-closed 35/36 speaker data migration contracts, without live writes."""

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import migrate_speaker_spacing_35_36 as m


class SpeakerMigrationContracts(unittest.TestCase):
    def test_revision_names_and_35_old_new_source_hashes(self):
        self.assertEqual(m.MARKERS, (
            "035-I-B:transcript-speaker:v1", "036-I-B:transcript-speaker:v1"))
        self.assertEqual(m.ORIGINAL_SNAPSHOT_35,
                         "631c4fb71784439961956b582d0e66847eb862e2c2370f49c89d5ca520057c35")
        self.assertEqual(m.FROZEN_35_SHA,
                         "946faa08bcca6613fada345c190e72b45da79da55ebd8762529fdcd207e0df3f")
        self.assertIn("FOR EACH ROW", m._GUARD_TRIGGER)
        self.assertIn("NEW.question_id LIKE '035-I-L-%'", m._GUARD_FUNCTION)
        self.assertIn("NEW.question_id LIKE '036-I-L-%'", m._GUARD_FUNCTION)
        self.assertIn("ERRCODE = '23514'", m._GUARD_FUNCTION)

    def test_verified_backup_requires_fresh_restore_and_content_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            binary = folder / "topik.dump"
            binary.write_bytes(b"actual backup content")
            sha = hashlib.sha256(binary.read_bytes()).hexdigest()
            now = datetime.now(timezone.utc)
            manifest = folder / "proof.json"
            proof = {
                "backup_sha256": sha, "preflight_sha256": "f" * 64,
                "restore_verified": True, "restore_details": "22 tables and 1324 rows verified",
                "restored_database": "topik_speaker_restore_fixture",
                "backed_up_at": (now - timedelta(minutes=20)).isoformat(),
                "restored_at": (now - timedelta(minutes=10)).isoformat(),
            }

            def attempt(candidate):
                manifest.write_text(json.dumps(candidate), encoding="utf-8")
                return m.verified_backup(binary.resolve(), manifest, "f" * 64)

            self.assertEqual(attempt(proof), proof)
            wrong = {**proof, "backup_sha256": "0" * 64}
            with self.assertRaisesRegex(m.MigrationBlocked, "Backup SHA"):
                attempt(wrong)
            missing_db = {**proof}
            del missing_db["restored_database"]
            with self.assertRaisesRegex(m.MigrationBlocked, "restore"):
                attempt(missing_db)
            future = {**proof, "restored_at": (now + timedelta(days=4)).isoformat()}
            with self.assertRaisesRegex(m.MigrationBlocked, "24 hours"):
                attempt(future)
            expired = {**proof, "backed_up_at": (now - timedelta(days=3)).isoformat()}
            with self.assertRaisesRegex(m.MigrationBlocked, "24 hours"):
                attempt(expired)
            missing_provenance = {**proof, "preflight_sha256": "0" * 64}
            with self.assertRaisesRegex(m.MigrationBlocked, "source snapshot"):
                attempt(missing_provenance)

    def test_speaker_rule_must_not_modify_user_punctuation(self):
        from src.extraction_rules import normalize_speaker_turn_spacing
        current = "\ub0a8\uc790 :\uadf8\ub798\uc694? \uadf8\ub7fc, \uc6b0\ub9ac\ub294 \uac00\uc694."
        updated = normalize_speaker_turn_spacing(current)
        self.assertEqual(updated, "\ub0a8\uc790: \uadf8\ub798\uc694? \uadf8\ub7fc, \uc6b0\ub9ac\ub294 \uac00\uc694.")
        self.assertEqual(normalize_speaker_turn_spacing(updated), updated)


if __name__ == "__main__":
    unittest.main()
