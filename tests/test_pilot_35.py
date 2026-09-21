"""Offline regressions for the 35th TOPIK I prototype importer.

Writes only into TemporaryDirectory; never modifies the source corpus or the
user's existing derived pilot database.
"""

import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src import pilot_35
from src.review_ui import ReviewStore


class TestPilot35(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (pilot_35.SESSION_DIR / "35th-TOPIK-I-Answer-Sheet.pdf").exists():
            raise unittest.SkipTest("Local private 35th TOPIK I corpus is not installed")

    def test_preview_and_original_answer_pdf_agree(self):
        answer_path = pilot_35.SESSION_DIR / "35th-TOPIK-I-Answer-Sheet.pdf"
        table = pilot_35.pdf_answer_table(answer_path)
        questions, groups, images = pilot_35.load_preview(next(pilot_35.SESSION_DIR.glob("*.html")))
        self.assertEqual((len(questions), len(groups), len(images)), (70, 26, 6))
        self.assertEqual(set(table), {"listening", "reading"})
        for question in questions:
            local = question["number"] - (30 if question["section"] == "reading" else 0)
            choice, points, _ = table[question["section"]][local]
            self.assertEqual((question["answer"], question["points"]), (choice, points))

    def test_database_counts_provenance_images_transcripts_and_idempotency(self):
        with tempfile.TemporaryDirectory(prefix="topik-35-test-") as tmp:
            directory = Path(tmp)
            db_path, report_path = directory / "pilot.sqlite", directory / "report.json"
            with patch.object(pilot_35, "OUTPUT_DIR", directory), \
                 patch.object(pilot_35, "DB_PATH", db_path), \
                 patch.object(pilot_35, "REPORT_PATH", report_path):
                first = pilot_35.main()
                self.assertFalse(first["reused_existing_database"])
                self.assertEqual((first["questions"], first["choices"], first["answers"],
                                  first["transcripts"], first["image_blobs"]), (70, 280, 70, 30, 11))
                self.assertEqual(first["active_image_blobs"], 6)
                self.assertEqual(first["image_question_links"], 7)
                self.assertEqual(first["review_status"], {"needs_manual_review": 70})
                before = hashlib.sha256(db_path.read_bytes()).hexdigest()
                with closing(sqlite3.connect(db_path)) as connection:
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(connection.execute("SELECT answer_key_number FROM questions "
                                                        "WHERE exam_number=31").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT answer_key_number FROM questions "
                                                        "WHERE exam_number=70").fetchone()[0], 40)
                    self.assertEqual(connection.execute("SELECT count(*) FROM audio_assets "
                                                        "WHERE timing_status='not_segmented'").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT count(*) FROM images "
                                                        "WHERE substr(hex(bytes),1,16)='89504E470D0A1A0A'").fetchone()[0], 11)
                    shared = connection.execute("SELECT dialogue_text FROM transcripts t JOIN questions q "
                                                "ON q.id=t.question_id WHERE q.exam_number IN (29,30) "
                                                "ORDER BY q.exam_number").fetchall()
                    self.assertEqual(shared[0], shared[1])
                    self.assertEqual(connection.execute("SELECT count(*) FROM questions "
                                                        "WHERE review_status='verified'").fetchone()[0], 0)
                second = pilot_35.main()
                self.assertTrue(second["reused_existing_database"])
                self.assertEqual(hashlib.sha256(db_path.read_bytes()).hexdigest(), before)

    def test_existing_database_changed_source_metadata_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="topik-35-reject-") as tmp:
            directory = Path(tmp)
            db_path, report_path = directory / "pilot.sqlite", directory / "report.json"
            with patch.object(pilot_35, "OUTPUT_DIR", directory), \
                 patch.object(pilot_35, "DB_PATH", db_path), \
                 patch.object(pilot_35, "REPORT_PATH", report_path):
                pilot_35.main()
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute("UPDATE import_metadata SET value='invalid' WHERE key='preview_sha256'")
                    connection.commit()
                altered_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
                with self.assertRaisesRegex(RuntimeError, "will not be overwritten"):
                    pilot_35.main()
                self.assertEqual(hashlib.sha256(db_path.read_bytes()).hexdigest(), altered_hash)

    def test_importer_reuse_preserves_real_human_review(self):
        with tempfile.TemporaryDirectory(prefix="topik-35-reviewed-") as tmp:
            directory = Path(tmp)
            db_path, report_path = directory / "pilot.sqlite", directory / "report.json"
            with patch.object(pilot_35, "OUTPUT_DIR", directory), \
                 patch.object(pilot_35, "DB_PATH", db_path), \
                 patch.object(pilot_35, "REPORT_PATH", report_path):
                pilot_35.main()
                store = ReviewStore(db_path, root=pilot_35.ROOT)
                item = store.get_question("035-I-R-031")
                saved = store.save_review(item["id"], {
                    "version": item["version"], "status": "verified", "stem": item["stem"],
                    "choices": [choice["text"] for choice in item["choices"]],
                    "transcript_text": None, "note": "Source PDF and answer verified by reviewer",
                })
                self.assertEqual(saved["review_status"], "verified")
                before = hashlib.sha256(db_path.read_bytes()).hexdigest()
                second = pilot_35.main()
                self.assertTrue(second["reused_existing_database"])
                self.assertEqual(second["review_status"], {"needs_manual_review": 69, "verified": 1})
                self.assertEqual(hashlib.sha256(db_path.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
