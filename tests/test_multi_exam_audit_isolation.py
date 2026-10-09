"""Regression: adding another session must not alter the frozen 35th audit."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src import ai_audit_35, pilot_35


class MultiExamAuditIsolationTests(unittest.TestCase):
    def test_adding_36_exam_sources_groups_images_does_not_change_35_snapshot(self):
        original = pilot_35.DB_PATH.resolve()
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "multi-exam-test.sqlite"
            with closing(sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(path)) as dst:
                    src.backup(dst)
            before = ai_audit_35.create_source_snapshot(path)["snapshot_sha256"]
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("INSERT INTO exams VALUES(?,?,?,?)", ("036-I-B", 36, "I", "B"))
                db.execute(
                    "INSERT INTO sections VALUES(?,?,?,?,?,?)",
                    ("036-I-B-listening", "036-I-B", "listening", 1, 30, 0),
                )
                db.execute(
                    "INSERT INTO question_groups(id,section_id,first_exam_number,last_exam_number,instruction,"
                    "passage_text,points_each,passage_image_key) VALUES(?,?,?,?,?,?,?,?)",
                    ("036-I-L-01-04", "036-I-B-listening", 1, 4, "new 36 instruction", "", 4, None),
                )
                db.execute(
                    "INSERT INTO source_files(relative_path,kind,sha256,byte_size,source_url,source_page) "
                    "VALUES(?,?,?,?,?,?)",
                    ("topik-past-papers/36th/mock-paper.pdf", "test_paper", "a" * 64, 1234, None, None),
                )
                db.execute(
                    "INSERT INTO images(key,mime_type,sha256,bytes,source_file_id) "
                    "VALUES(?,?,?,?,(SELECT id FROM source_files WHERE relative_path=?))",
                    ("036-I-L-001-img", "image/png", "b" * 64, b"placeholder", "topik-past-papers/36th/mock-paper.pdf"),
                )
                db.commit()
            after = ai_audit_35.create_source_snapshot(path)["snapshot_sha256"]
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
