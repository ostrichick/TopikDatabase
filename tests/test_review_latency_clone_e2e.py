"""Opt-in real PostgreSQL review test; writes ONLY to an owned disposable clone.

Set TOPIK_E2E_DATABASE_URL to a verified `topik_perf_...` database, never the
production `topik`. This test is intentionally skipped in normal test runs.
It must not modify the immutable PDFs/MP3s or operational reviewer tables.
"""

from __future__ import annotations

import os
import base64
import http.client
import json
import secrets
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlsplit

from src.database import connect_postgres, get_media_root
from src.review_ui import Conflict, ReviewStore, ThreadingHTTPServer, make_handler


E2E_URL = os.environ.get("TOPIK_E2E_DATABASE_URL", "").strip()


@unittest.skipUnless(E2E_URL, "Explicit isolated TOPIK_E2E_DATABASE_URL required")
class ReviewLatencyCloneE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parsed = urlsplit(E2E_URL)
        database_name = unquote(parsed.path.lstrip("/"))
        if not database_name.startswith("topik_perf_"):
            raise RuntimeError("Refuse review POST outside topik_perf_ disposable DB")
        with connect_postgres(E2E_URL, readonly=True) as conn:
            actual_name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
            if actual_name != database_name:
                raise RuntimeError("URL database identity mismatch; refuse test writes")
            rows = conn.execute("SELECT count(*) AS n FROM questions").fetchone()["n"]
            if rows != 140:
                raise RuntimeError("Unexpected review test fixture; refuse writes")
        cls.store = ReviewStore(database_url=E2E_URL, media_root=get_media_root(),
                                exam_id="036-I-B")

    def test_real_review_ack_stale_version_and_next_detail(self):
        qid = "036-I-L-020"
        current = self.store.get_question(qid, fast=True)
        previous_status = current["review_status"]
        self.assertIn(previous_status, ("needs_manual_review", "verified", "rejected"))
        self.assertIsNotNone(current["transcript"])
        payload = {"version": current["version"], "status": "verified",
                   "stem": current["stem"],
                   "choices": [item["text"] for item in current["choices"]],
                   "transcript_text": current["transcript"]["text"],
                   "note": "isolated_p4_latency_approval_test"}
        started = time.perf_counter()
        ack = self.store.save_review(qid, payload, fast_response=True)
        duration = (time.perf_counter() - started) * 1000
        self.assertEqual(ack["id"], qid)
        self.assertEqual(ack["review_status"], "verified")
        self.assertTrue(ack["saved"])
        self.assertEqual(ack["request_version"], current["version"])
        self.assertEqual(ack["version"], current["version"] + 1)
        self.assertGreaterEqual(duration, 0)
        refreshed = self.store.get_question(qid, fast=True)
        self.assertEqual(refreshed["version"], ack["version"])
        self.assertEqual(refreshed["review_status"], "verified")
        self.assertEqual(refreshed["transcript"]["text"], current["transcript"]["text"])
        self.assertTrue(any(entry["scope"] == "manual_question_review" for entry in refreshed["history"]))
        with self.assertRaises(Conflict):
            self.store.save_review(qid, payload, fast_response=True)
        after_stale = self.store.get_question(qid, fast=True)
        self.assertEqual(after_stale["version"], ack["version"])
        next_detail = self.store.get_question("036-I-L-021", fast=True)
        self.assertEqual(next_detail["number"], 21)
        print(f"ISOLATED_36_APPROVAL_ACK_MS={duration:.1f} STALE_409_AND_NEXT_DETAIL_PASS")

    def test_old_35_source_and_36_shared_pair_unchanged(self):
        store_35 = self.store.for_exam("035-I-B")
        for first in (25, 27, 29):
            a = store_35.get_question(f"035-I-L-{first:03d}", fast=True)
            b = store_35.get_question(f"035-I-L-{first + 1:03d}", fast=True)
            self.assertEqual(a["transcript"]["text"], b["transcript"]["text"])

    def test_actual_local_http_approval_ack_409_and_successor_get(self):
        """Exercise the production HTTP handler but never touch production DB."""
        access_key = secrets.token_urlsafe(32)
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     make_handler(self.store, access_key=access_key))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        authorization = "Basic " + base64.b64encode(
            f"operator:{access_key}".encode("utf-8")).decode("ascii")

        def request(path, *, payload=None, csrf=None):
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
            headers = {"Authorization": authorization}
            body = None
            if payload is not None:
                headers.update({"Content-Type": "application/json", "Origin": origin,
                                "X-Review-Token": csrf})
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                client.request("POST" if payload is not None else "GET", path,
                               headers=headers, body=body)
                response = client.getresponse()
                return response.status, json.loads(response.read().decode("utf-8"))
            finally:
                client.close()

        try:
            status, fast_list = request("/api/questions-fast?exam_id=036-I-B")
            self.assertEqual(status, 200)
            csrf = fast_list["csrf_token"]
            qid = "036-I-L-021"
            status, detail = request(f"/api/questions/{qid}?exam_id=036-I-B&fast=1")
            self.assertEqual(status, 200)
            self.assertEqual(detail["id"], qid)
            submitted = {"version": detail["version"], "status": "verified",
                         "stem": detail["stem"],
                         "choices": [c["text"] for c in detail["choices"]],
                         "transcript_text": detail["transcript"]["text"],
                         "note": "isolated_http_approval_navigation_test"}
            started = time.perf_counter()
            status, ack = request(f"/api/questions/{qid}/review?exam_id=036-I-B&fast=1",
                                  payload=submitted, csrf=csrf)
            elapsed = (time.perf_counter() - started) * 1000
            self.assertEqual(status, 200, ack)
            self.assertEqual(ack["review_status"], "verified")
            self.assertEqual(ack["version"], detail["version"] + 1)
            status, next_question = request("/api/questions/036-I-L-022?exam_id=036-I-B&fast=1")
            self.assertEqual(status, 200)
            self.assertEqual(next_question["id"], "036-I-L-022")
            status, conflict = request(f"/api/questions/{qid}/review?exam_id=036-I-B&fast=1",
                                       payload=submitted, csrf=csrf)
            self.assertEqual(status, 409, conflict)
            print(f"ISOLATED_HTTP_APPROVAL_ACK_MS={elapsed:.1f} HTTP_409_NEXT_GET_PASS")
        finally:
            server.shutdown()
            worker.join(timeout=8)
            server.server_close()
            self.assertFalse(worker.is_alive())

    def test_two_simultaneous_reviewers_only_one_cas_wins(self):
        qid = "036-I-L-023"
        detail = self.store.get_question(qid, fast=True)
        original_history_ids = {row["id"] for row in detail["history"]}
        payload = {"version": detail["version"], "status": "verified",
                   "stem": detail["stem"],
                   "choices": [c["text"] for c in detail["choices"]],
                   "transcript_text": detail["transcript"]["text"],
                   "note": "isolated_parallel_cas_test"}
        barrier = threading.Barrier(2)

        def submit(_index):
            second_store = ReviewStore(database_url=E2E_URL, media_root=get_media_root(),
                                       exam_id="036-I-B")
            barrier.wait(timeout=15)
            try:
                ack = second_store.save_review(qid, dict(payload), fast_response=True)
                return "committed", ack["version"]
            except Conflict:
                return "conflict", None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, range(2)))
        self.assertEqual(sorted(entry[0] for entry in results), ["committed", "conflict"])
        latest = self.store.get_question(qid, fast=True)
        self.assertEqual(latest["version"], detail["version"] + 1)
        new_history_ids = {row["id"] for row in latest["history"]} - original_history_ids
        self.assertEqual(len(new_history_ids), 1)
        self.assertEqual(latest["review_status"], "verified")
        print("ISOLATED_TWO_REVIEWER_CAS_SINGLE_COMMIT_PASS")


if __name__ == "__main__":
    unittest.main()
