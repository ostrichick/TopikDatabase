"""Subject-scoped audit execution must not leak between list rows."""

import unittest

from src import ai_audit_35
from src.review_ui import ReviewStore
from tests import test_ai_audit_35 as fixtures


class ReviewAuditListScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AIAudit35Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.db_path = self.fixture.db_path

    def test_shared_run_attempts_remain_scoped_in_question_list(self):
        before = self.fixture._human_state()
        run = ai_audit_35.create_run(self.db_path, label="subject-scope")
        listening = ai_audit_35.export_pass(
            self.db_path, run_id=run["run_id"], pass_number=1,
            auditor_id="listen-only", perspective="transcript_alignment",
            model_id="fixture", prompt_version="test",
        )
        independent = ai_audit_35.export_pass(
            self.db_path, run_id=run["run_id"], pass_number=2,
            auditor_id="all", perspective="independent",
            model_id="fixture", prompt_version="test",
        )
        ai_audit_35.record_attempt_outcome(
            self.db_path, listening["pass_id"], "timed_out",
            error_code="timeout", error_message="listening only",
        )
        # The independent pass remains pending: both rows share the same run
        # but only the listening subject has an attempt.
        store = ReviewStore(db_path=self.db_path)
        listing = store.list_questions()
        audit_by_id = {row["id"]: row.get("ai_audit", {}) for row in listing["items"]}
        listening_status = ai_audit_35.status_report(self.db_path, run["run_id"], subject_id=fixtures.Q1)
        reading_status = ai_audit_35.status_report(self.db_path, run["run_id"], subject_id=fixtures.Q2)
        self.assertEqual(audit_by_id[fixtures.Q1]["attempt_total"], listening_status["latest_run"]["attempt_total"])
        self.assertEqual(audit_by_id[fixtures.Q2]["attempt_total"], reading_status["latest_run"]["attempt_total"])
        self.assertEqual(audit_by_id[fixtures.Q1]["attempt_total"], 1)
        self.assertEqual(audit_by_id[fixtures.Q2]["attempt_total"], 0)
        self.assertEqual(audit_by_id[fixtures.Q1]["latest_run"]["subject_id"], fixtures.Q1)
        self.assertEqual(audit_by_id[fixtures.Q2]["latest_run"]["subject_id"], fixtures.Q2)
        self.assertTrue(audit_by_id[fixtures.Q1]["has_partial_failures"])
        self.assertFalse(audit_by_id[fixtures.Q2]["has_partial_failures"])
        self.assertEqual(self.fixture._human_state(), before)


if __name__ == "__main__":
    unittest.main()
