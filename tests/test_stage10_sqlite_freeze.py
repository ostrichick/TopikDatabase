"""Stage 10 SQLite freeze and no-fallback regressions."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scripts import stage10_sqlite_freeze as stage10
from src import ai_audit_35, review_ui, sqlite_archive
from src.database import DatabaseConfigError


class Stage10GuardTests(unittest.TestCase):
    def test_operational_reviewer_has_no_implicit_sqlite_fallback(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TOPIK_DATABASE_URL", None)
            with self.assertRaisesRegex(DatabaseConfigError, "implicit SQLite fallback is disabled"):
                review_ui.ReviewStore()

    def test_operational_ai_audit_has_no_implicit_sqlite_fallback(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TOPIK_DATABASE_URL", None)
            with self.assertRaisesRegex(DatabaseConfigError, "Stage 10 requires TOPIK_DATABASE_URL"):
                ai_audit_35._resolve_target()

    def test_canonical_sqlite_write_is_rejected(self):
        with self.assertRaisesRegex(sqlite_archive.FrozenSqliteError, "immutable rollback archive"):
            sqlite_archive.assert_sqlite_write_allowed(sqlite_archive.CANONICAL_SQLITE)

    def test_explicit_temporary_sqlite_fixture_remains_supported(self):
        with tempfile.TemporaryDirectory(prefix="topik-stage10-fixture-") as directory:
            path = Path(directory) / "fixture.sqlite"
            self.assertEqual(sqlite_archive.assert_sqlite_write_allowed(path), path.resolve())

    def test_hardlink_alias_and_open_connection_cannot_bypass_canonical_guard(self):
        with tempfile.TemporaryDirectory(prefix="topik-stage10-alias-") as directory:
            root = Path(directory)
            canonical = root / "canonical.sqlite"
            alias = root / "alias.sqlite"
            with closing(sqlite3.connect(canonical)) as db:
                db.execute("CREATE TABLE sample(value TEXT)")
                db.commit()
            os.link(canonical, alias)
            with patch.object(sqlite_archive, "CANONICAL_SQLITE", canonical.resolve()):
                with self.assertRaisesRegex(sqlite_archive.FrozenSqliteError, "immutable rollback archive"):
                    sqlite_archive.assert_sqlite_write_allowed(alias)
                with closing(sqlite3.connect(alias)) as db:
                    with self.assertRaisesRegex(sqlite_archive.FrozenSqliteError, "already-open"):
                        sqlite_archive.assert_sqlite_connection_write_allowed(db)


class Stage10ArchiveTests(unittest.TestCase):
    def test_freeze_and_verify_archive_without_changing_bytes(self):
        with tempfile.TemporaryDirectory(prefix="topik-stage10-root-") as root_dir, \
                tempfile.TemporaryDirectory(prefix="topik-stage10-runtime-") as runtime_dir:
            root = Path(root_dir)
            derived = root / "topik-past-papers" / "derived"
            derived.mkdir(parents=True)
            canonical = derived / "035-I-B.sqlite"
            before = derived / "035-I-B.before-test.sqlite"
            for path, value in ((canonical, "canonical"), (before, "before")):
                with closing(sqlite3.connect(path)) as db:
                    db.execute("CREATE TABLE sample(value TEXT)")
                    db.execute("INSERT INTO sample VALUES(?)", (value,))
                    db.commit()
            canonical_hash = stage10._sha256(canonical)
            before_hash = stage10._sha256(before)
            runtime = Path(runtime_dir)
            (runtime / "operational.json").write_text(json.dumps({
                "database_url": "postgresql://topik_app@wordpress-blog:55432/topik?sslmode=verify-full",
                "media_root": str(root / "topik-past-papers"),
            }), encoding="utf-8")

            with patch.object(stage10, "ROOT", root), \
                    patch.object(stage10, "DERIVED", derived), \
                    patch.object(stage10, "CANONICAL", canonical), \
                    patch.object(stage10, "EXPECTED_SQLITE_SHA256", canonical_hash), \
                    patch.object(stage10, "_verify_approved_source", return_value=None):
                try:
                    result = stage10.freeze(runtime)
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(result["frozen_source_files"], 2)
                    self.assertEqual(stage10._sha256(canonical), canonical_hash)
                    self.assertEqual(stage10._sha256(before), before_hash)
                    self.assertFalse(canonical.stat().st_mode & stat.S_IWRITE)
                    self.assertFalse(before.stat().st_mode & stat.S_IWRITE)
                    self.assertEqual(stage10.verify(runtime)["archive_copies"], 2)
                finally:
                    # TemporaryDirectory cannot remove read-only Windows files.
                    for path in [canonical, before, *(runtime / "sqlite-archive" / "files").glob("*.sqlite")]:
                        if path.exists():
                            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)

    def test_freeze_does_not_publish_frozen_manifest_before_readonly_succeeds(self):
        with tempfile.TemporaryDirectory(prefix="topik-stage10-root-") as root_dir, \
                tempfile.TemporaryDirectory(prefix="topik-stage10-runtime-") as runtime_dir:
            root = Path(root_dir)
            derived = root / "topik-past-papers" / "derived"
            derived.mkdir(parents=True)
            canonical = derived / "035-I-B.sqlite"
            with closing(sqlite3.connect(canonical)) as db:
                db.execute("CREATE TABLE sample(value TEXT)")
                db.commit()
            canonical_hash = stage10._sha256(canonical)
            runtime = Path(runtime_dir)
            (runtime / "operational.json").write_text(json.dumps({
                "database_url": "postgresql://topik_app@wordpress-blog:55432/topik?sslmode=verify-full",
                "media_root": str(root / "topik-past-papers"),
            }), encoding="utf-8")
            with patch.object(stage10, "ROOT", root), \
                    patch.object(stage10, "DERIVED", derived), \
                    patch.object(stage10, "CANONICAL", canonical), \
                    patch.object(stage10, "EXPECTED_SQLITE_SHA256", canonical_hash), \
                    patch.object(stage10, "_verify_approved_source", return_value=None), \
                    patch.object(stage10, "_set_readonly", side_effect=OSError("simulated chmod failure")):
                with self.assertRaisesRegex(OSError, "simulated chmod failure"):
                    stage10.freeze(runtime)
            self.assertFalse((runtime / "sqlite-archive" / "stage10-manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
