"""Stage-8 PostgreSQL clip/export contract tests using a mapping-row PG double.

The immutable production SQLite and MP3 are read only. A disposable SQLite copy
provides PostgreSQL-like mapping rows and records the SQL lock contract; live
PostgreSQL concurrency/error behavior is validated separately during stage 8.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src import database, review_ui


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
SOURCE_AUDIO = ROOT / "topik-past-papers" / "35th" / "35-TOPIK-I-Listening-Audio-File.mp3"
SCHEMA = ROOT / "db" / "schema.sql"
FAKE_MP3 = b"ID3" + b"\x00" * 2048


class StaticCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.index = 0

    def fetchone(self):
        if self.index >= len(self.rows):
            return None
        row = self.rows[self.index]
        self.index += 1
        return row

    def fetchall(self):
        if self.index >= len(self.rows):
            return []
        rows = self.rows[self.index:]
        self.index = len(self.rows)
        return rows

    def __iter__(self):
        return iter(self.rows)


class PgSQLiteConnection:
    """SQLite-backed mapping-row connection that records PostgreSQL lock SQL."""

    def __init__(self, path: Path, *, writable: bool, calls: list,
                 fail_review_insert: list[bool], fail_commit: list[bool]):
        if writable:
            self.db = sqlite3.connect(path, timeout=5)
        else:
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            self.db.execute("PRAGMA query_only=ON")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.calls = calls
        self.fail_review_insert = fail_review_insert
        self.fail_commit = fail_commit

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        if "information_schema.tables" in sql:
            return StaticCursor([{"present": True}])
        if self.fail_review_insert[0] and sql.startswith("INSERT INTO review_records"):
            self.fail_review_insert[0] = False
            raise database.DatabaseOperationError("forced review history failure")
        # SQLite cannot parse FOR UPDATE, but the original SQL remains recorded
        # above so tests can assert the Stage-6 PostgreSQL lock order.
        return self.db.execute(sql.replace(" FOR UPDATE", ""), params)

    def commit(self):
        if self.fail_commit[0]:
            self.fail_commit[0] = False
            raise database.DatabaseOperationError("forced commit failure")
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        self.db.close()


@unittest.skipUnless(SOURCE_DB.is_file() and SOURCE_AUDIO.is_file(), "Private 35-I pilot media is unavailable")
class Stage8PostgresClipTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="topik-stage8-pg-clip-")
        self.addCleanup(self.sandbox.cleanup)
        self.base = Path(self.sandbox.name)
        self.db_path = self.base / "review.sqlite"
        with closing(sqlite3.connect(SOURCE_DB.resolve().as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(self.db_path)) as destination:
                original.backup(destination)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript(SCHEMA.read_text(encoding="utf-8"))
            db.execute("UPDATE audio_segments SET status='verified',clip_relative_path=NULL,clip_sha256=NULL")
            db.commit()
        self.calls: list[tuple[str, tuple]] = []
        self.fail_review_insert = [False]
        self.fail_commit = [False]
        self.pc_media = self._media_root("pc-media")
        self.laptop_media = self._media_root("laptop-media")

    def _media_root(self, name: str, *, copy_source: bool = False) -> Path:
        media = self.base / name
        source_dir = media / "35th"
        source_dir.mkdir(parents=True)
        target = source_dir / SOURCE_AUDIO.name
        if copy_source:
            shutil.copyfile(SOURCE_AUDIO, target)
        else:
            os.link(SOURCE_AUDIO, target)
        return media

    def _store(self, media_root: Path) -> review_ui.ReviewStore:
        # Initialize against the disposable SQLite copy, then exercise only the
        # PostgreSQL branches through our PG mapping-row connection double.
        store = review_ui.ReviewStore(self.db_path, root=ROOT)
        store.backend = "postgres"
        store.database_url = "postgresql://fixture.invalid/topik"
        store.media_root = media_root.resolve()
        store.source_root = (store.media_root / "35th").resolve()
        store._connect = lambda writable=False: PgSQLiteConnection(  # type: ignore[method-assign]
            self.db_path, writable=writable, calls=self.calls,
            fail_review_insert=self.fail_review_insert, fail_commit=self.fail_commit,
        )
        return store

    @staticmethod
    def _fake_encoder(payload: bytes = FAKE_MP3, before_write=None):
        def encode(source, destination, start_ms, end_ms, *, expected_source_sha256=None):
            if before_write is not None:
                before_write()
            if expected_source_sha256 != review_ui.ReviewStore._file_sha256(SOURCE_AUDIO):
                raise AssertionError("source SHA preflight was not forwarded to the encoder")
            Path(destination).write_bytes(payload)
        return encode

    def _rows(self, *numbers):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(db.execute(
                "SELECT * FROM audio_segments WHERE question_id=?",
                (f"035-I-L-{number:03d}",),
            ).fetchone()) for number in numbers]

    def _history_count(self, *numbers):
        qids = [f"035-I-L-{number:03d}" for number in numbers]
        with closing(sqlite3.connect(self.db_path)) as db:
            placeholders = ",".join("?" for _ in qids)
            return db.execute(
                f"SELECT COUNT(*) FROM review_records WHERE subject_type='audio_segment' "
                f"AND scope='audio_export_35' AND subject_id IN ({placeholders})", qids,
            ).fetchone()[0]

    def _human_state(self, number: int):
        qid = f"035-I-L-{number:03d}"
        with closing(sqlite3.connect(self.db_path)) as db:
            return (
                db.execute("SELECT stem,review_status,points FROM questions WHERE id=?", (qid,)).fetchone(),
                db.execute("SELECT dialogue_text,review_status FROM transcripts WHERE question_id=?", (qid,)).fetchone(),
            )

    def test_first_export_encodes_before_pg_locks_and_links_shared_pair_atomically(self):
        store = self._store(self.pc_media)
        baseline = (self._human_state(25), self._human_state(26))
        events = []

        def encoder(*args, **kwargs):
            events.append("encode")
            return self._fake_encoder()(*args, **kwargs)

        original_locked = store._locked_audio_rows

        def locked(db, qids):
            events.append("lock")
            return original_locked(db, qids)

        with patch("src.audio_35.export_segment", side_effect=encoder), \
             patch.object(store, "_locked_audio_rows", side_effect=locked):
            result = store.export_audio_clip("035-I-L-025")

        self.assertLess(events.index("encode"), events.index("lock"))
        left, right = self._rows(25, 26)
        self.assertEqual((left["clip_relative_path"], left["clip_sha256"]),
                         (right["clip_relative_path"], right["clip_sha256"]))
        self.assertEqual(left["clip_sha256"], hashlib.sha256(FAKE_MP3).hexdigest())
        self.assertEqual(self._history_count(25, 26), 2)
        self.assertEqual((self._human_state(25), self._human_state(26)), baseline)
        self.assertTrue((self.pc_media / Path(left["clip_relative_path"]).relative_to("topik-past-papers")).is_file())
        self.assertEqual(result["audio_segment"]["clip_url"], "/media/035-I-L-025/clip")
        lock_sql = [sql for sql, _ in self.calls if "FOR UPDATE" in sql]
        self.assertTrue(any("ORDER BY id FOR UPDATE" in sql for sql in lock_sql))
        self.assertTrue(any("ORDER BY question_id FOR UPDATE" in sql for sql in lock_sql))

    def test_laptop_missing_local_clip_rematerializes_without_duplicate_history(self):
        pc = self._store(self.pc_media)
        laptop = self._store(self.laptop_media)
        with patch("src.audio_35.export_segment", side_effect=self._fake_encoder()):
            pc.export_audio_clip("035-I-L-025")
        canonical = self._rows(25)[0]
        history = self._history_count(25, 26)
        self.assertIsNone(laptop.get_question("035-I-L-025")["audio_segment"]["clip_url"])

        with patch("src.audio_35.export_segment", side_effect=self._fake_encoder()) as encoder:
            result = laptop.export_audio_clip("035-I-L-025")
        self.assertEqual(encoder.call_count, 1)
        self.assertEqual(self._history_count(25, 26), history)
        self.assertEqual(self._rows(25)[0]["clip_sha256"], canonical["clip_sha256"])
        self.assertEqual(result["audio_segment"]["clip_url"], "/media/035-I-L-025/clip")

    def test_rematerialize_mismatch_never_changes_canonical_metadata_or_history(self):
        pc = self._store(self.pc_media)
        laptop = self._store(self.laptop_media)
        with patch("src.audio_35.export_segment", side_effect=self._fake_encoder()):
            pc.export_audio_clip("035-I-L-025")
        before = self._rows(25, 26)
        history = self._history_count(25, 26)

        with patch("src.audio_35.export_segment", side_effect=self._fake_encoder(b"ID3" + b"x" * 2048)):
            with self.assertRaisesRegex(review_ui.Conflict, "checksum differs"):
                laptop.export_audio_clip("035-I-L-025")
        self.assertEqual(self._rows(25, 26), before)
        self.assertEqual(self._history_count(25, 26), history)
        canonical_path = laptop._clip_path_from_relative(before[0]["clip_relative_path"])
        self.assertFalse(canonical_path.exists())
        self.assertEqual(list(laptop._clip_output_dir().glob(".audio-export-*.mp3")), [])

    def test_existing_destination_same_sha_reused_different_sha_conflicts_without_overwrite(self):
        store = self._store(self.pc_media)
        row = self._rows(1)[0]
        sha = hashlib.sha256(FAKE_MP3).hexdigest()
        name = f"035-I-L-001-v{row['version']}-{sha[:12]}.mp3"
        destination = store._clip_output_dir(create=True) / name
        destination.write_bytes(FAKE_MP3)
        before_mtime = destination.stat().st_mtime_ns
        with patch("src.audio_35.export_segment", side_effect=self._fake_encoder()):
            store.export_audio_clip("035-I-L-001")
        self.assertEqual(destination.stat().st_mtime_ns, before_mtime)
        self.assertEqual(self._rows(1)[0]["clip_sha256"], sha)

        # A second disposable question with a pre-existing wrong destination at
        # the generated canonical name must fail without overwriting those bytes.
        row2 = self._rows(2)[0]
        name2 = f"035-I-L-002-v{row2['version']}-{sha[:12]}.mp3"
        destination2 = store._clip_output_dir() / name2
        wrong = b"not-the-generated-audio"
        destination2.write_bytes(wrong)
        with patch("src.audio_35.export_segment", side_effect=self._fake_encoder()):
            with self.assertRaisesRegex(review_ui.Conflict, "different audio"):
                store.export_audio_clip("035-I-L-002")
        self.assertEqual(destination2.read_bytes(), wrong)
        self.assertIsNone(self._rows(2)[0]["clip_relative_path"])

    def test_stale_segment_after_encode_is_rejected_before_link(self):
        store = self._store(self.pc_media)

        def mutate_segment():
            with closing(sqlite3.connect(self.db_path)) as db:
                db.execute("UPDATE audio_segments SET version=version+1 WHERE question_id='035-I-L-001'")
                db.commit()

        with patch("src.audio_35.export_segment", side_effect=self._fake_encoder(before_write=mutate_segment)):
            with self.assertRaisesRegex(review_ui.Conflict, "changed while exporting"):
                store.export_audio_clip("035-I-L-001")
        row = self._rows(1)[0]
        self.assertIsNone(row["clip_relative_path"])
        self.assertEqual(self._history_count(1), 0)
        self.assertEqual(list(store._clip_output_dir().glob(".audio-export-*.mp3")), [])

    def test_source_mismatch_blocks_encoder_and_source_change_during_encode_blocks_link(self):
        media = self._media_root("mutable-media", copy_source=True)
        store = self._store(media)
        local_source = media / "35th" / SOURCE_AUDIO.name
        with local_source.open("ab") as stream:
            stream.write(b"x")
        with patch("src.audio_35.export_segment") as encoder:
            with self.assertRaises(review_ui.ReviewError):
                store.export_audio_clip("035-I-L-001")
        encoder.assert_not_called()
        self.assertIsNone(self._rows(1)[0]["clip_relative_path"])

        # Restore a clean independent copy, then mutate it only after the
        # encoder was allowed to run. The post-encode source recheck must stop
        # publication/linking.
        local_source.unlink()
        shutil.copyfile(SOURCE_AUDIO, local_source)

        def mutating_encoder(source, destination, start_ms, end_ms, *, expected_source_sha256=None):
            Path(destination).write_bytes(FAKE_MP3)
            with Path(source).open("ab") as stream:
                stream.write(b"changed-during-encode")

        with patch("src.audio_35.export_segment", side_effect=mutating_encoder):
            with self.assertRaisesRegex(review_ui.Conflict, "changed while exporting"):
                store.export_audio_clip("035-I-L-001")
        self.assertIsNone(self._rows(1)[0]["clip_relative_path"])
        self.assertEqual(self._history_count(1), 0)
        self.assertEqual(list(store._clip_output_dir().glob(".audio-export-*.mp3")), [])

    def test_unsafe_canonical_path_is_rejected_before_encoder(self):
        store = self._store(self.pc_media)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "UPDATE audio_segments SET clip_relative_path=?,clip_sha256=? WHERE question_id='035-I-L-001'",
                ("topik-past-papers/derived/audio-clips/../escape.mp3", "0" * 64),
            )
            db.commit()
        with patch("src.audio_35.export_segment") as encoder:
            with self.assertRaisesRegex(review_ui.ReviewError, "Unsafe exported clip path"):
                store.export_audio_clip("035-I-L-001")
        encoder.assert_not_called()

    def test_clip_output_symlink_escape_is_rejected_before_outside_directory_creation(self):
        store = self._store(self.pc_media)
        outside = self.base / "outside"
        outside.mkdir()
        derived = self.pc_media / "derived"
        try:
            os.symlink(outside, derived, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        with self.assertRaisesRegex(review_ui.ReviewError, "Unsafe audio clip output directory"):
            store._clip_output_dir(create=True)
        self.assertFalse((outside / "audio-clips").exists())

    def test_encoder_failure_leaves_no_db_link_or_temporary_clip(self):
        from src.audio_35 import AudioError

        store = self._store(self.pc_media)
        with patch("src.audio_35.export_segment", side_effect=AudioError("forced encoder failure")):
            with self.assertRaisesRegex(review_ui.ReviewError, "Audio clip export failed"):
                store.export_audio_clip("035-I-L-001")
        self.assertIsNone(self._rows(1)[0]["clip_relative_path"])
        self.assertEqual(self._history_count(1), 0)
        self.assertEqual(list(store._clip_output_dir().glob(".audio-export-*.mp3")), [])

    def test_review_history_failure_rolls_back_clip_link_without_retry_or_partial_pair(self):
        store = self._store(self.pc_media)
        self.fail_review_insert[0] = True
        with patch("src.audio_35.export_segment", side_effect=self._fake_encoder()) as encoder:
            with self.assertRaises(database.DatabaseOperationError):
                store.export_audio_clip("035-I-L-025")
        self.assertEqual(encoder.call_count, 1)
        left, right = self._rows(25, 26)
        self.assertIsNone(left["clip_relative_path"])
        self.assertIsNone(right["clip_relative_path"])
        self.assertEqual(self._history_count(25, 26), 0)
        self.assertEqual(list(store._clip_output_dir().glob(".audio-export-*.mp3")), [])
        self.assertEqual(list(store._clip_output_dir().glob("*.mp3")), [])

    def test_commit_failure_cleans_only_owned_publication_and_rolls_back_link(self):
        store = self._store(self.pc_media)
        self.fail_commit[0] = True
        with patch("src.audio_35.export_segment", side_effect=self._fake_encoder()):
            with self.assertRaises(database.DatabaseOperationError):
                store.export_audio_clip("035-I-L-001")
        self.assertIsNone(self._rows(1)[0]["clip_relative_path"])
        self.assertEqual(self._history_count(1), 0)
        self.assertEqual(list(store._clip_output_dir().glob("*.mp3")), [])

    def test_segment_edit_invalidates_existing_canonical_link(self):
        store = self._store(self.pc_media)
        with patch("src.audio_35.export_segment", side_effect=self._fake_encoder()):
            store.export_audio_clip("035-I-L-001")
        current = store.get_question("035-I-L-001")["audio_segment"]
        updated = store.save_audio_segment("035-I-L-001", {
            "version": current["version"],
            "start_ms": current["start_ms"] + 100,
            "end_ms": current["end_ms"] + 100,
            "status": "candidate",
        })
        self.assertIsNone(self._rows(1)[0]["clip_relative_path"])
        self.assertIsNone(updated["audio_segment"]["clip_url"])


if __name__ == "__main__":
    unittest.main()
