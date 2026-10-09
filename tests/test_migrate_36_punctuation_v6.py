"""Frozen 36th v6 punctuation migration: no operational database required."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import migrate_36_punctuation_v6 as m


class Fixture:
    @classmethod
    def setUpClass(cls):
        cls.before, cls.after = m.read_frozen_staging()
        cls.changes = m.build_changes(cls.before, cls.after)


class StagingContract(Fixture, unittest.TestCase):
    def test_exact_frozen_signature_and_unmodified_nontext_fields(self):
        self.assertEqual(len(self.changes), 33)
        self.assertEqual(sum(len(c.after) - len(c.before) for c in self.changes), 109)
        self.assertEqual({f"{c.table}.{c.field}" for c in self.changes},
                         {"question_groups.instruction", "transcripts.dialogue_text"})
        self.assertEqual(m._staging_rows(self.before).keys(), m._staging_rows(self.after).keys())
        self.assertEqual(len(m._staging_rows(self.before)), 502)
        self.assertEqual(self.before["sources"], self.after["sources"])
        for a, b in zip(self.before["questions"], self.after["questions"], strict=True):
            for field in ("raw_question_text", "review_status", "images", "answer", "points"):
                self.assertEqual(a.get(field), b.get(field))

    def test_reject_unapproved_content_or_forged_normalization(self):
        with self.assertRaisesRegex(m.MigrationBlocked, "exact approved target"):
            tampered = copy.deepcopy(self.after)
            tampered["sources"][0]["sha256"] = "0" * 64
            m.build_changes(self.before, tampered)
        with self.assertRaisesRegex(m.MigrationBlocked, "exact frozen baseline"):
            tampered = copy.deepcopy(self.before)
            tampered["questions"][0]["raw_question_text"] += "CORRUPTION"
            m.build_changes(tampered, self.after)
        with self.assertRaisesRegex(m.MigrationBlocked, "exact approved target"):
            tampered = copy.deepcopy(self.after)
            tampered["questions"][0]["answer"]["choice_number"] = 4
            m.build_changes(self.before, tampered)


class FakePG:
    """Transactional fake: commit and rollback track complete mutation state."""

    def __init__(self, rows):
        self.rows = dict(rows)
        self.marker = None
        self.calls = []
        self.commit_count = 0
        self.rollback_count = 0
        self.closed = False
        self.updates = 0
        self.fail_at = None
        self.fail_after_marker = False
        self.stale_at = None
        self.review_hash = "r" * 64
        self._committed = (dict(self.rows), None)

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if sql.startswith("SET TRANSACTION") or sql.startswith("LOCK TABLE"):
            return SimpleNamespace(rowcount=0)
        if sql.startswith("SELECT id FROM exams"):
            return SimpleNamespace(fetchone=lambda: {"id": m.EXAM_ID})
        if sql.startswith("SELECT value FROM import_metadata"):
            return SimpleNamespace(fetchone=lambda: (
                {"value": self.marker} if self.marker is not None else None))
        if sql.startswith("INSERT INTO import_metadata"):
            self.marker = params[1]
            if self.fail_after_marker:
                raise RuntimeError("failure after marker insertion")
            return SimpleNamespace(rowcount=1)
        if sql.startswith("UPDATE "):
            self.updates += 1
            if self.updates == self.fail_at:
                raise RuntimeError("injected PG failure")
            table = sql.split()[1]
            field = sql.split("SET ")[1].split("=")[0]
            key = (table, params[1], params[3] if table == "choices" else None, field)
            if self.stale_at == self.updates or self.rows.get(key) != params[2]:
                return SimpleNamespace(rowcount=0)
            self.rows[key] = params[0]
            return SimpleNamespace(rowcount=1)
        raise AssertionError(f"Unexpected SQL: {sql}")

    def commit(self):
        self.commit_count += 1
        self._committed = (dict(self.rows), self.marker)

    def rollback(self):
        self.rollback_count += 1
        self.rows, self.marker = dict(self._committed[0]), self._committed[1]

    def close(self):
        self.closed = True


class FakeMigration(Fixture, unittest.TestCase):
    def setUp(self):
        self.pg = FakePG(m._staging_rows(self.before))
        self.initial = dict(self.pg.rows)
        self.patches = [
            patch.object(m, "connect_postgres", side_effect=lambda *args, **kwargs: self.pg),
            patch.object(m, "_snapshot_35", return_value=m.FROZEN_35_SHA),
            patch.object(m, "_review_snapshot", side_effect=lambda db: db.review_hash),
            patch.object(m, "_text_rows", side_effect=lambda db, lock: dict(db.rows)),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def execute_migration(self, *, apply=False, backup=None):
        return m.run(self.before, self.after, apply=apply,
                     backup=backup, url="postgresql://dummy.invalid/not-used")

    def backup(self):
        return m.BackupProof("a" * 64, self.pg.review_hash, "/fake/verified-backup")

    def test_default_dry_run_is_read_only_and_retains_nine_approvals(self):
        result = self.execute_migration()
        self.assertEqual((result["status"], result["mode"]), ("pending", "dry_run"))
        self.assertEqual(result["expected_db_fields"], 502)
        self.assertEqual((result["changed_cells"], result["inserted_spaces"]), (33, 109))
        self.assertEqual(self.pg.rows, self.initial)
        self.assertIsNone(self.pg.marker)
        self.assertFalse(any(sql.startswith("LOCK TABLE") or sql.startswith("UPDATE ")
                             for sql, _ in self.pg.calls))
        self.assertEqual(self.pg.commit_count, 0)
        self.assertTrue(self.pg.closed)

    def test_apply_rejects_missing_verified_backup_before_connect(self):
        with self.assertRaisesRegex(m.MigrationBlocked, "backup proof"):
            self.execute_migration(apply=True)
        self.assertFalse(self.pg.calls)

    def test_approved_reviews_preserved_and_second_apply_is_idempotent(self):
        first = self.execute_migration(apply=True, backup=self.backup())
        self.assertEqual(first["status"], "applied")
        self.assertEqual(self.pg.rows, m._staging_rows(self.after))
        self.assertEqual(self.pg.commit_count, 1)
        self.assertEqual(self.pg.updates, 33)
        self.assertIn("v5-to-v6", first["migration_marker_key"])
        self.assertTrue(any(sql.startswith("LOCK TABLE") for sql, _ in self.pg.calls))
        stored_marker = self.pg.marker
        # A new human review after migration must not invalidate replay of the
        # already-applied marker, because replay cannot write any text/history.
        self.pg.review_hash = "z" * 64
        again = self.execute_migration(apply=True, backup=self.backup())
        self.assertEqual(again["status"], "already_applied")
        self.assertEqual(self.pg.marker, stored_marker)
        self.assertEqual(self.pg.commit_count, 1)
        self.assertEqual(self.pg.updates, 33)

    def test_exact_all_field_baseline_blocks_human_rewording_or_mapping_drift(self):
        self.pg.rows[("questions", "036-I-L-001", None, "raw_question_text")] += "X"
        with self.assertRaisesRegex(m.MigrationBlocked, "all 502 frozen v5 fields"):
            self.execute_migration(apply=True, backup=self.backup())
        self.assertEqual(self.pg.updates, 0)
        self.pg.rows = dict(self.initial)
        self.pg.rows.pop(next(k for k in self.pg.rows if k[0] == "choices"))
        with self.assertRaisesRegex(m.MigrationBlocked, "mapping"):
            self.execute_migration(apply=True, backup=self.backup())
        self.assertEqual(self.pg.updates, 0)

    def test_unknown_marker_or_partial_data_blocks_replay(self):
        self.pg.marker = "wrong value"
        with self.assertRaisesRegex(m.MigrationBlocked, "marker evidence differs"):
            self.execute_migration(apply=True, backup=self.backup())
        self.pg.marker = None
        self.pg.rows = dict(m._staging_rows(self.after))
        with self.assertRaisesRegex(m.MigrationBlocked, "frozen v5 fields"):
            self.execute_migration(apply=True, backup=self.backup())

    def test_changed_review_history_since_backup_blocks_first_update(self):
        backup = self.backup()
        self.pg.review_hash = "9" * 64
        with self.assertRaisesRegex(m.MigrationBlocked, "reviews have changed"):
            self.execute_migration(apply=True, backup=backup)
        self.assertEqual(self.pg.updates, 0)
        self.assertEqual(self.pg.rows, self.initial)

    def test_atomic_rollback_on_mid_write_conflict_and_error(self):
        for inject in ("stale", "error", "marker", "review", "35"):
            with self.subTest(inject=inject):
                self.pg = FakePG(self.initial)
                if inject == "stale": self.pg.stale_at = 7
                if inject == "error": self.pg.fail_at = 7
                if inject == "marker": self.pg.fail_after_marker = True
                patches = []
                if inject == "review":
                    checks = iter([self.pg.review_hash, "changed"])
                    patches.append(patch.object(m, "_review_snapshot", side_effect=lambda db: next(checks)))
                if inject == "35":
                    hashes = iter([m.FROZEN_35_SHA, "modified"])
                    patches.append(patch.object(m, "_snapshot_35", side_effect=lambda db: next(hashes)))
                for p in patches: p.start()
                try:
                    with self.assertRaises((m.MigrationBlocked, RuntimeError)):
                        self.execute_migration(apply=True, backup=self.backup())
                finally:
                    for p in reversed(patches): p.stop()
                self.assertEqual(self.pg.rows, self.initial)
                self.assertIsNone(self.pg.marker)
                self.assertEqual(self.pg.commit_count, 0)
                self.assertGreater(self.pg.rollback_count, 0)


class ReviewedStateSnapshot(unittest.TestCase):
    def test_nine_human_approvals_allowed_and_history_content_is_hashed(self):
        questions = [{"id": f"036-I-{'L' if n <= 30 else 'R'}-{n:03d}",
                      "review_status": "verified" if n <= 9 else "needs_manual_review"}
                     for n in range(1, 71)]
        transcripts = [{"question_id": questions[i]["id"],
                        "review_status": "verified" if i < 9 else "needs_manual_review"}
                       for i in range(30)]
        audio = [{"question_id": questions[0]["id"], "status": "candidate"}]
        history = [{"id": i, "subject_type": "question", "subject_id": questions[i-1]["id"],
                    "scope": "manual_question_review", "status": "verified", "evidence": f"note-{i}"}
                   for i in range(1, 10)]

        class Probe:
            def execute(self, sql, params=()):
                if sql.startswith("SELECT id,review_status FROM questions"):
                    rows = questions
                elif sql.startswith("SELECT question_id,review_status"):
                    rows = transcripts
                elif "FROM audio_segments" in sql:
                    self_assert = sql.strip().startswith("SELECT question_id,audio_asset_id")
                    if not self_assert:
                        raise AssertionError("audio_segments has no id column")
                    rows = audio
                elif "FROM review_records" in sql:
                    rows = history
                else:
                    raise AssertionError(sql)
                return SimpleNamespace(fetchall=lambda: rows)

        probe = Probe()
        original = m._review_snapshot(probe)
        self.assertEqual(len(original), 64)
        self.assertEqual(original, m._review_snapshot(probe))
        history[2]["evidence"] = "human note edited"
        self.assertNotEqual(original, m._review_snapshot(probe))


class BackupEvidence(unittest.TestCase):
    def test_manifest_requires_real_file_sha_restore_and_recent_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="topik-v6-backup-evidence-") as temp:
            directory = Path(temp)
            backup = directory / "verified.dump"
            backup.write_bytes(b"synthetic backup, used only for manifest verification test")
            now = datetime.now(timezone.utc)
            evidence = {
                "backup_path": str(backup),
                "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                "backed_up_at": (now - timedelta(minutes=20)).isoformat(),
                "restored_at": (now - timedelta(minutes=10)).isoformat(),
                "restore_verified": True,
                "restore_details": "isolated PG restore and parity confirmed (test fixture only)",
                "source_v5_sha256": m.SOURCE_V5_SHA256,
                "35th_source_sha256": m.FROZEN_35_SHA,
                "review_snapshot_sha256": "1" * 64,
            }
            manifest = directory / "evidence.json"

            def load():
                manifest.write_text(json.dumps(evidence), encoding="utf-8")
                return m.read_backup_proof(manifest)

            self.assertEqual(load().review_snapshot_sha256, "1" * 64)
            evidence["restore_verified"] = False
            with self.assertRaisesRegex(m.MigrationBlocked, "restore"):
                load()
            evidence["restore_verified"] = True
            evidence["backup_sha256"] = "0" * 64
            with self.assertRaisesRegex(m.MigrationBlocked, "SHA-256"):
                load()
            evidence["backup_sha256"] = hashlib.sha256(backup.read_bytes()).hexdigest()
            evidence["backed_up_at"] = (now - timedelta(days=3)).isoformat()
            with self.assertRaisesRegex(m.MigrationBlocked, "old"):
                load()


if __name__ == "__main__":
    unittest.main()
