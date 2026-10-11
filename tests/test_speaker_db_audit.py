"""Speaker-spacing preflight may inspect snapshots but must never write SQL."""

import unittest

from scripts.audit_speaker_db import audit


class FakeReadOnlyDb:
    def __init__(self):
        self.rows = []
        for exam in ("035-I-B", "036-I-B"):
            for n in range(1, 31):
                before = ("남자:안녕하세요.\n여자:네." if exam == "035-I-B" and n == 1
                          else "남자: 안녕하세요.\n여자: 네." if exam == "035-I-B"
                          else "남자 :안녕하세요.\n여자 :네.")
                self.rows.append({
                    "exam_id": exam,
                    "question_id": f"{exam[:3]}-I-L-{n:03d}",
                    "dialogue_text": before,
                    "transcript_status": "verified" if exam == "035-I-B" else "needs_manual_review",
                    "question_status": "verified" if exam == "035-I-B" else "needs_manual_review",
                })
        self.statements = []

    def execute(self, sql, params):
        self.statements.append(sql)
        assert sql.lstrip().startswith("SELECT")
        assert params == ("035-I-B", "036-I-B")
        return self

    def fetchall(self):
        return self.rows


class ReadOnlySpeakerAuditTests(unittest.TestCase):
    def test_35_single_transcript_and_36_thirty_affected_without_writes(self):
        db = FakeReadOnlyDb()
        snapshot = [dict(row) for row in db.rows]
        report = audit(db)
        self.assertEqual(report["mode"], "read_only")
        self.assertEqual(report["exam_sessions"]["035-I-B"]["affected_question_ids"],
                         ["035-I-L-001"])
        self.assertEqual(report["exam_sessions"]["036-I-B"]["affected_count"], 30)
        self.assertEqual(len(db.statements), 1)
        self.assertEqual(db.rows, snapshot)

    def test_mismatched_shared_source_is_not_silently_accepted(self):
        db = FakeReadOnlyDb()
        db.rows[25]["dialogue_text"] += "잘못된 추가 대사"
        with self.assertRaisesRegex(ValueError, "Shared transcript pair"):
            audit(db)


if __name__ == "__main__":
    unittest.main()
