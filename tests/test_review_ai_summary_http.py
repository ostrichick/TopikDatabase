"""Exercise new AI summary GET endpoint over disposable localhost HTTP."""

import json
import sqlite3
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from src.review_ui import ReviewStore, make_handler
from tests import test_ai_audit_35 as fixtures


class AuditSummaryHttpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AIAudit35Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        with closing(sqlite3.connect(self.fixture.db_path)) as db:
            db.execute("UPDATE questions SET preview_flags_json=? WHERE id=?", (
                json.dumps([{"severity": "review", "code": "source_check", "message": "Check PDF"}]),
                fixtures.Q1,
            ))
            db.execute("UPDATE transcripts SET warnings_json=? WHERE question_id=?", (
                json.dumps([{"severity": "info", "code": "transcript_source", "message": "Check transcript"}]),
                fixtures.Q1,
            ))
            db.commit()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ReviewStore(db_path=self.fixture.db_path)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _get(self, endpoint):
        url = f"http://127.0.0.1:{self.server.server_port}/{endpoint}"
        with urlopen(url, timeout=5) as response:
            self.assertEqual(response.status, 200)
            return json.load(response)

    def test_fast_list_and_audit_summary_are_separate_live_http_contracts(self):
        fast = self._get("api/questions-fast")
        self.assertEqual(len(fast["items"]), 2)
        self.assertIn("review_version", fast["items"][0])
        audit = self._get("api/questions-ai-summary")
        self.assertEqual(audit["exam_id"], "035-I-B")
        self.assertEqual(audit["state"], "ready")
        self.assertEqual(audit["items"], [])
        self.assertNotIn("csrf_token", audit)
        detail = self._get(f"api/questions/{fixtures.Q1}")
        self.assertEqual(detail["preview_flags"][0]["code"], "source_check")
        self.assertEqual(detail["transcript"]["warnings"][0]["code"], "transcript_source")


if __name__ == "__main__":
    unittest.main()
