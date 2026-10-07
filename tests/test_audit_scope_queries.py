"""Remote list reads must avoid per-question work outside frozen audit scope."""
import unittest
from unittest.mock import patch

from src import ai_audit_35
from tests import test_ai_audit_35 as fixtures

Q2 = fixtures.Q2


class AuditScopeQueries(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AIAudit35Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.path = self.fixture.db_path

    def test_no_audits_skip_every_question_summary(self):
        with patch.object(ai_audit_35, "summarize_question", wraps=ai_audit_35.summarize_question) as summary:
            result = ai_audit_35.summarize_all_questions(self.path)
        self.assertEqual(result["questions"], [])
        summary.assert_not_called()

    def test_pending_scope_is_preserved_without_scanning_unrelated_questions(self):
        run = ai_audit_35.create_run(self.path, auditors=["scope-check"], model_id="fixture", subject_ids=[Q2])
        self.assertEqual(ai_audit_35.audit_subject_ids(self.path), {Q2})
        self.assertEqual(ai_audit_35.audit_subject_ids(self.path, run["run_id"]), {Q2})
        with patch.object(ai_audit_35, "summarize_question", wraps=ai_audit_35.summarize_question) as summary:
            ai_audit_35.summarize_all_questions(self.path)
        self.assertEqual([call.args[1] for call in summary.call_args_list], [Q2])


if __name__ == "__main__":
    unittest.main()
