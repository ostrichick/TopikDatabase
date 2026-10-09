"""Disposable database tests of the 35th read-only punctuation overlay audit."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scripts import audit_35_punctuation_v3 as audit


def fixture_db(path: Path, *, unexpected_extra_group=False, wrong_exam=False,
               include_other_exam=False):
    """Create a synthetic 35th review schema with stable 432 display fields."""
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE exams(id TEXT PRIMARY KEY,session INTEGER,level TEXT,booklet TEXT);
            CREATE TABLE sections(id TEXT PRIMARY KEY,exam_id TEXT,name TEXT);
            CREATE TABLE question_groups(id TEXT PRIMARY KEY,section_id TEXT,
                                         instruction TEXT,passage_text TEXT);
            CREATE TABLE questions(id TEXT PRIMARY KEY,section_id TEXT,stem TEXT);
            CREATE TABLE choices(question_id TEXT,number INTEGER,text TEXT);
            CREATE TABLE transcripts(question_id TEXT,dialogue_text TEXT);
            CREATE TABLE review_records(id INTEGER PRIMARY KEY,subject_id TEXT,status TEXT);
        """)
        exam = "036-I-B" if wrong_exam else "035-I-B"
        session = 36 if wrong_exam else 35
        db.execute("INSERT INTO exams VALUES(?,?,?,?)", (exam, session, "I", "B"))
        db.executemany("INSERT INTO sections VALUES(?,?,?)", [
            (exam+"-listening", exam, "listening"),
            (exam+"-reading", exam, "reading"),
        ])
        prefix = exam.rsplit("-", 1)[0]
        for n in range(26):
            if n == 0:
                gid = prefix+"-L-07-10"
                text = "어디입니까?<보기>"
            elif n == 1:
                gid = prefix+"-L-11-14"
                text = "무엇입니까?<보기>"
            elif n == 2:
                gid = prefix+"-R-31-33"
                text = "말씀하세요?<보기>" if unexpected_extra_group else "이미 올바릅니다. <보기>"
            else:
                gid = prefix+f"-G-{n:03}"
                text = "문항을 읽고 고르십시오."
            db.execute("INSERT INTO question_groups VALUES(?,?,?,?)",
                       (gid, exam+"-listening", text, "문장입니다. 다음 문장"))
        for n in range(1, 71):
            section = "L" if n <= 30 else "R"
            qid = f"{prefix}-{section}-{n:03}"
            db.execute("INSERT INTO questions VALUES(?,?,?)", (
                qid, exam+("-listening" if n <= 30 else "-reading"), "문장입니다."
            ))
            db.executemany("INSERT INTO choices VALUES(?,?,?)",
                           [(qid, k, "선택지") for k in range(1, 5)])
            if n <= 30:
                # One colon and one question mark are missing spaces.
                dialogue = "남자 :공책이에요?그럼" if n == 1 else "남자 : 공책이에요? 그럼"
                db.execute("INSERT INTO transcripts VALUES(?,?)", (qid, dialogue))
        db.execute("INSERT INTO review_records(subject_id,status) VALUES(?,?)",
                   (f"{prefix}-L-001", "verified"))
        if include_other_exam:
            db.execute("INSERT INTO exams VALUES('036-I-B',36,'I','B')")
            db.execute("INSERT INTO sections VALUES('other-listening','036-I-B','listening')")
            db.execute("INSERT INTO questions VALUES('036-I-L-001','other-listening','왜?그럼')")
            db.execute("INSERT INTO transcripts VALUES('036-I-L-001','여자 :공책')")
        db.commit()


class OverlayCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="topik35-punctuation-audit-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "fixture.sqlite"
        self.output = self.folder / "proposed.json"

    def _run(self, *, exam_id=audit.EXAM_ID):
        # The production tool permits JSON only within the ignored derived
        # directory. Redirect that fixed root to a disposable test directory.
        with patch.object(audit, "DEFAULT_REPORT", self.output):
            return audit.run(sqlite_db=self.source, exam_id=exam_id, output=self.output)

    def _rows(self, sql: str):
        with closing(sqlite3.connect(self.source)) as db:
            return db.execute(sql).fetchall()

    def test_exact_three_fields_four_spaces_with_immutable_source(self):
        fixture_db(self.source, include_other_exam=True)
        original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        original_review = self._rows("SELECT subject_id,status FROM review_records")
        result = self._run()
        self.assertEqual(result["source_backend"], "sqlite")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["may_apply_to_database"])
        self.assertEqual(result["changed_fields"], 3)
        self.assertEqual(result["spaces_added"], 4)
        self.assertEqual(result["display_fields_checked"], 432)
        self.assertEqual(result["source_counts"], audit.EXPECTED_COUNTS)
        self.assertEqual({
            (c["table"], c["id"], c["field"]) for c in result["candidates"]
        }, audit.EXPECTED_CHANGE_KEYS)
        self.assertEqual(result["changes_by_field"],
                         {"question_groups.instruction": 2,
                          "transcripts.dialogue_text": 1})
        self.assertEqual(self._rows("SELECT subject_id,status FROM review_records"), original_review)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original_hash)
        saved = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(saved["source_display_cells_sha256"], result["source_display_cells_sha256"])
        for item in saved["candidates"]:
            self.assertTrue(item["display_only"])
            self.assertEqual(len(item["preview"]["source_sha256"]), 64)
            self.assertEqual(len(item["preview"]["overlay_sha256"]), 64)
            self.assertNotEqual(item["preview"]["before"], item["preview"]["after"])

    def test_historical_archive_drift_aborts_without_overwriting_report(self):
        fixture_db(self.source, unexpected_extra_group=True)
        self.output.write_text("previous-safe-report", encoding="utf-8")
        original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with self.assertRaisesRegex(audit.AuditBlocked, "fields=4, spaces=5"):
            self._run()
        self.assertEqual(self.output.read_text(encoding="utf-8"), "previous-safe-report")
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original_hash)

    def test_wrong_exam_rejected_before_any_read_or_write(self):
        fixture_db(self.source)
        with self.assertRaisesRegex(audit.AuditBlocked, "035-I-B"):
            self._run(exam_id="036-I-B")
        self.assertFalse(self.output.exists())
        self.source.unlink()
        fixture_db(self.source, wrong_exam=True)
        with self.assertRaisesRegex(Exception, "035-I-B|unavailable"):
            self._run()
        self.assertFalse(self.output.exists())

    def test_missing_question_fails_count_guard(self):
        fixture_db(self.source)
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("DELETE FROM questions WHERE id='035-I-R-070'")
            db.commit()
        with self.assertRaisesRegex(audit.AuditBlocked, "unexpected 35th source counts"):
            self._run()
        self.assertFalse(self.output.exists())

    def test_readonly_sqlite_connection_rejects_mutations(self):
        fixture_db(self.source)
        with audit._readonly_store("035-I-B", sqlite_db=self.source) as (backend, db):
            self.assertEqual(backend, "sqlite")
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE questions SET stem='bad' WHERE id='035-I-L-001'")
        self.assertEqual(self._rows(
            "SELECT stem FROM questions WHERE id='035-I-L-001'"
        ), [("문장입니다.",)])

    def test_report_output_cannot_escape_derived_folder(self):
        fixture_db(self.source)
        with self.assertRaisesRegex(audit.AuditBlocked, "fixed ignored derived JSON"):
            audit.run(sqlite_db=self.source, output=self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
