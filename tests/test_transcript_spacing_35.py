"""Integration regressions for transcript spacing migration, on SQLite backups only.

The installed 35th pilot DB is opened read-only as the backup *source*. Every
upgrade writes to a separate TemporaryDirectory, never to the user's database.
"""

from __future__ import annotations

import hashlib
import importlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.extraction_rules import normalize_punctuation_spacing
from src.review_ui import Conflict, ReviewStore
from src.transcript_35 import extract_transcripts


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
SOURCE_PDF = ROOT / "topik-past-papers" / "35th" / "35th-TOPIK-I-Listening-Transcript.pdf"
Q1 = "035-I-L-001"
Q2 = "035-I-L-002"


class TestTranscriptSpacingMigration35(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SOURCE_DB.is_file() or not SOURCE_PDF.is_file():
            raise unittest.SkipTest("Private 35th pilot DB and listening PDF are required")
        try:
            cls.upgrade_module = importlib.import_module("src.upgrade_transcripts_35")
        except ModuleNotFoundError as error:
            if error.name == "src.upgrade_transcripts_35":
                raise unittest.SkipTest("Transcript spacing migration is not implemented yet") from error
            raise
        cls.expected = extract_transcripts(SOURCE_PDF, restore_visual_spacing=True)

    def setUp(self):
        self.original_db_hash = hashlib.sha256(SOURCE_DB.read_bytes()).hexdigest()
        self.original_pdf_hash = hashlib.sha256(SOURCE_PDF.read_bytes()).hexdigest()
        self.sandbox = tempfile.TemporaryDirectory(prefix="topik-transcript-upgrade-test-")
        self.addCleanup(self.sandbox.cleanup)
        self.db_path = Path(self.sandbox.name) / "copy.sqlite"
        self.backup_path = Path(self.sandbox.name) / "before-upgrade.sqlite"
        # SQLite's online backup ensures a valid snapshot even if the live
        # source is being accessed concurrently by a reviewer.
        with closing(sqlite3.connect(SOURCE_DB.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(self.db_path)) as target:
                source.backup(target)
        # The live database has already been migrated. Reconstruct the historical
        # pre-upgrade transcript state ONLY in this disposable copy so the tests
        # remain meaningful after the user's real migration and later reviews.
        with closing(sqlite3.connect(self.db_path)) as fixture:
            version = fixture.execute(
                "SELECT value FROM import_metadata WHERE key='transcript_extraction_version'"
            ).fetchone()
            if version is not None:
                historical = extract_transcripts(SOURCE_PDF, restore_visual_spacing=False)
                fixture.execute("DELETE FROM import_metadata WHERE key='transcript_extraction_version'")
                for number in range(2, 31):
                    qid = f"035-I-L-{number:03d}"
                    fixture.execute("UPDATE questions SET review_status='needs_manual_review' WHERE id=?", (qid,))
                    fixture.execute(
                        "UPDATE transcripts SET dialogue_text=?,review_status='needs_manual_review' WHERE question_id=?",
                        (normalize_punctuation_spacing(historical[number]["text"]), qid),
                    )
                    fixture.execute(
                        "DELETE FROM review_records WHERE subject_type='question' AND subject_id=? "
                        "AND scope IN ('manual_question_review','transcript_word_spacing_35')", (qid,),
                    )
                fixture.commit()
        self.store = ReviewStore(self.db_path, root=ROOT)
        self.original_q1 = self._question_state(Q1)
        self.original_answers = self._answers()
        self.original_statuses = self._statuses()
        self.original_versions = {qid: self.store.get_question(qid)["version"]
                                  for qid in (Q1, Q2)}

    def tearDown(self):
        self.assertEqual(hashlib.sha256(SOURCE_DB.read_bytes()).hexdigest(), self.original_db_hash)
        self.assertEqual(hashlib.sha256(SOURCE_PDF.read_bytes()).hexdigest(), self.original_pdf_hash)

    def _question_state(self, qid):
        with closing(sqlite3.connect(self.db_path)) as conn:
            question = conn.execute("SELECT id,review_status,stem,raw_question_text "
                                    "FROM questions WHERE id=?", (qid,)).fetchone()
            transcript = conn.execute("SELECT dialogue_text,review_status,warnings_json,source_pdf_page "
                                      "FROM transcripts WHERE question_id=?", (qid,)).fetchone()
            history = conn.execute("SELECT id,status,reviewer,scope,evidence,reviewed_at "
                                   "FROM review_records WHERE subject_type='question' AND subject_id=? "
                                   "ORDER BY id", (qid,)).fetchall()
            return question, transcript, history

    def _answers(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute("SELECT question_id,choice_number,source_file_id,source_pdf_page,"
                                "preview_and_pdf_agree FROM answers ORDER BY question_id").fetchall()

    def _statuses(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return dict(conn.execute("SELECT id,review_status FROM questions"))

    def _all_transcripts(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return dict(conn.execute("SELECT q.exam_number,t.dialogue_text FROM transcripts t "
                                     "JOIN questions q ON q.id=t.question_id ORDER BY q.exam_number"))

    def _history_counts(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return dict(conn.execute("SELECT subject_id,COUNT(*) FROM review_records "
                                     "WHERE subject_type='question' GROUP BY subject_id"))

    def _upgrade(self):
        return self.upgrade_module.upgrade_db(db_path=self.db_path, backup_path=self.backup_path)

    def test_preserves_verified_q1_and_corrects_only_29_unreviewed_transcripts(self):
        self.assertEqual(self.original_statuses[Q1], "verified")
        self.assertGreater(len(self.original_q1[2]), 0, "Fixture must include Q1 human review history")
        original_transcripts = self._all_transcripts()
        original_history = self._history_counts()
        self._upgrade()

        # A human-approved transcript is never silently replaced, even if
        # another PDF-derived representation looks more polished.
        self.assertEqual(self._question_state(Q1), self.original_q1)
        self.assertEqual(self.store.get_question(Q1)["version"], self.original_versions[Q1])
        self.assertEqual(self._answers(), self.original_answers)
        self.assertEqual(self._statuses(), self.original_statuses)

        migrated = self._all_transcripts()
        self.assertEqual(set(migrated), set(range(1, 31)))
        self.assertEqual(migrated[1], original_transcripts[1])
        for number in range(2, 31):
            with self.subTest(question=number):
                self.assertTrue(migrated[number].strip())
                self.assertEqual(migrated[number], self.expected[number]["text"])
                self.assertNotEqual(migrated[number], original_transcripts[number])
        self.assertEqual(migrated[25], migrated[26])
        self.assertEqual(migrated[27], migrated[28])
        self.assertEqual(migrated[29], migrated[30])
        history = self._history_counts()
        self.assertEqual(history.get(Q1, 0), original_history.get(Q1, 0))
        self.assertGreater(history.get(Q2, 0), original_history.get(Q2, 0))
        self.assertTrue(self.backup_path.is_file(), "Explicit backup_path should create an original snapshot")

    def test_upgrade_is_idempotent_and_preserves_answer_and_history(self):
        self._upgrade()
        after = self.db_path.read_bytes()
        transcripts = self._all_transcripts()
        history = self._history_counts()
        self._upgrade()
        self.assertEqual(self.db_path.read_bytes(), after)
        self.assertEqual(self._all_transcripts(), transcripts)
        self.assertEqual(self._history_counts(), history)
        self.assertEqual(self._answers(), self.original_answers)
        self.assertEqual(self._question_state(Q1), self.original_q1)

    def test_stale_reviewer_version_conflicts_after_migration(self):
        stale = self.store.get_question(Q2)
        stale_payload = {
            "version": stale["version"],
            "status": stale["review_status"],
            "stem": stale["stem"],
            "choices": [choice["text"] for choice in stale["choices"]],
            "transcript_text": stale["transcript"]["text"],
            "note": "",
        }
        self._upgrade()
        self.assertGreater(self.store.get_question(Q2)["version"], stale["version"])
        first_revision = self._question_state(Q2)
        with self.assertRaises(Conflict):
            self.store.save_review(Q2, stale_payload)
        self.assertEqual(self._question_state(Q2), first_revision)
        self.assertEqual(self._answers(), self.original_answers)

    def test_unlogged_user_draft_is_skipped_without_overwrite(self):
        qid = "035-I-L-003"
        draft = "User-authored transcript draft; do not replace this text."
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("UPDATE transcripts SET dialogue_text=? WHERE question_id=?", (draft, qid))
            connection.commit()
        original = self._question_state(qid)
        result = self._upgrade()
        self.assertIn(3, result["skipped_edited"])
        self.assertEqual(self._question_state(qid), original)
        self.assertEqual(self._question_state(Q1), self.original_q1)
        self.assertEqual(self._answers(), self.original_answers)
        self.assertEqual(self._all_transcripts()[2], self.expected[2]["text"])


if __name__ == "__main__":
    unittest.main()
