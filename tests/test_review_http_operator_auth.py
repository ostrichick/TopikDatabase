"""Per-launch HTTP Basic operator access policy, on disposable TOPIK data.

Every test opens the frozen 35th SQLite in mode=ro/query_only and copies it
using SQLite backup to a temporary database. The 36th fixture is added ONLY
to that copy. All HTTP listeners bind 127.0.0.1 and an OS-assigned port.

The shared operator credential authorizes entry to the local application;
it does NOT attest who actually performed a question or audio review.
No production PostgreSQL, original media, source or existing tests are edited.
"""

from __future__ import annotations

import base64
import http.client
import inspect
import io
import json
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src import review_ui


ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
Q35 = "035-I-R-031"
Q36 = "036-I-R-031"


def _add_disposable_36th(db: sqlite3.Connection) -> None:
    """Make a distinguishable 36th reading row without copying real media."""
    db.execute("INSERT INTO exams(id,session,level,booklet) VALUES(?,?,?,?)",
               ("036-I-B", 36, "I", "B"))
    db.execute(
        "INSERT INTO sections(id,exam_id,name,first_exam_number,last_exam_number,answer_key_offset) "
        "SELECT ?,?,name,first_exam_number,last_exam_number,answer_key_offset "
        "FROM sections WHERE id=?",
        ("036-I-B-reading", "036-I-B", "035-I-B-reading"),
    )
    db.execute(
        "INSERT INTO questions("
        "id,section_id,group_id,source_file_id,exam_number,answer_key_number,"
        "source_pdf_page,printed_page,points,stem,raw_question_text,passage_id,"
        "requires_image,review_status,extraction_origin,preview_flags_json) "
        "SELECT ?,?,NULL,source_file_id,exam_number,answer_key_number,"
        "source_pdf_page,printed_page,points,?,raw_question_text,passage_id,"
        "requires_image,'needs_manual_review',extraction_origin,preview_flags_json "
        "FROM questions WHERE id=?",
        (Q36, "036-I-B-reading", "36th isolated test reading question", Q35),
    )
    db.execute(
        "INSERT INTO choices(question_id,number,text) "
        "SELECT ?,number,text FROM choices WHERE question_id=?", (Q36, Q35),
    )
    db.execute(
        "INSERT INTO answers(question_id,choice_number,source_file_id,"
        "source_pdf_page,preview_and_pdf_agree) "
        "SELECT ?,choice_number,source_file_id,source_pdf_page,preview_and_pdf_agree "
        "FROM answers WHERE question_id=?", (Q36, Q35),
    )
    db.commit()


class OperatorHTTPAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FROZEN.is_file():
            raise unittest.SkipTest("Frozen 35th SQLite is unavailable")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="topik-operator-http-")
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "isolated.sqlite"
        with closing(sqlite3.connect(FROZEN.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            source.execute("PRAGMA query_only=ON")
            self.assertEqual(source.execute("PRAGMA query_only").fetchone()[0], 1)
            with closing(sqlite3.connect(self.db_path)) as target:
                source.backup(target)
        with closing(sqlite3.connect(self.db_path)) as disposable:
            _add_disposable_36th(disposable)

        self.store = review_ui.ReviewStore(self.db_path, root=ROOT)
        self.secret = secrets.token_urlsafe(32)
        self.port = self._new_server(self.secret)
        self.origin = f"http://127.0.0.1:{self.port}"

    def _new_server(self, secret: str) -> int:
        server = review_ui.ThreadingHTTPServer(
            ("127.0.0.1", 0), review_ui.make_handler(self.store, access_key=secret),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(timeout=6)
            server.server_close()
            self.assertFalse(thread.is_alive(), "Temporary HTTP server did not stop")

        self.addCleanup(stop)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        self.assertGreater(server.server_port, 0)
        return server.server_port

    @staticmethod
    def _basic(password: str, username: str = "operator") -> str:
        raw = f"{username}:{password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _request(self, method: str, path: str, *, auth: str | None = "correct",
                 headers: dict | None = None, payload: dict | None = None,
                 port: int | None = None):
        chosen_port = port or self.port
        outbound = dict(headers or {})
        if auth == "correct":
            outbound["Authorization"] = self._basic(self.secret)
        elif auth == "wrong":
            outbound["Authorization"] = self._basic(self.secret + "-invalid")
        elif auth is not None:
            outbound["Authorization"] = auth
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None:
            outbound.setdefault("Content-Type", "application/json")
        client = http.client.HTTPConnection("127.0.0.1", chosen_port, timeout=8)
        try:
            client.request(method, path, headers=outbound, body=body)
            response = client.getresponse()
            raw = response.read()
            meta = {key.lower(): value for key, value in response.getheaders()}
            try:
                content = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                content = raw
            return response.status, meta, content
        finally:
            client.close()

    def _json(self, path: str):
        status, headers, content = self._request("GET", path)
        self.assertEqual(status, 200, content)
        self.assertIsInstance(content, dict)
        return content, headers

    @staticmethod
    def _payload(detail: dict, *, note: str = "Disposable local human review declaration"):
        return {
            "version": detail["version"],
            "status": "verified",
            "stem": detail["stem"],
            "choices": [choice["text"] for choice in detail["choices"]],
            "transcript_text": detail["transcript"]["text"] if detail["transcript"] else None,
            "note": note,
        }

    def _count_reviews(self, qid: str = Q35):
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute(
                "SELECT COUNT(*) FROM review_records WHERE subject_type='question' AND subject_id=?",
                (qid,),
            ).fetchone()[0]

    def _assert_challenge(self, status, headers, content):
        self.assertEqual(status, 401, content)
        self.assertRegex(headers.get("www-authenticate", ""), r"(?i)^Basic\b")
        self.assertNotIn("csrf_token", str(content))
        self.assertNotIn(self.secret, str(content))

    def test_every_get_post_and_media_route_challenges_before_disclosing_content(self):
        protected = (
            "/", "/api/questions", "/api/questions-fast", "/api/questions-bundle",
            "/api/questions-ai-summary", "/api/independent-audits",
            f"/api/questions/{Q35}", f"/api/questions/{Q35}?fast=1",
            f"/media/{Q35}/paper", f"/media/{Q35}/answer",
            "/media/035-I-L-001/audio", "/media/035-I-L-001/image/0",
            "/unrecognized/path", "/api/questions-fast?exam_id=036-I-B",
        )
        for route in protected:
            with self.subTest(route=route):
                result = self._request("GET", route, auth=None)
                self._assert_challenge(*result)
        data, _ = self._json("/api/questions-fast")
        token = data["csrf_token"]
        detail, _ = self._json(f"/api/questions/{Q35}?fast=1")
        before = self._count_reviews()
        headers = {"Origin": self.origin, "X-Review-Token": token}
        for route in (f"/api/questions/{Q35}/review?fast=1",
                      "/api/questions/035-I-L-025/audio-segment",
                      "/api/questions/035-I-L-025/export-clip"):
            with self.subTest(route=route):
                for auth in (None, "wrong"):
                    response = self._request("POST", route, auth=auth,
                                             payload=self._payload(detail), headers=headers)
                    self._assert_challenge(*response)
        self.assertEqual(self._count_reviews(), before)

    def test_wrong_identity_password_and_malformed_authorization_all_fail_closed(self):
        attempts = (
            None, "wrong", self._basic(self.secret, "not-operator"),
            "Bearer " + self.secret, "Basic !!invalid!!",
            "Basic " + base64.b64encode(b"operator-no-colon").decode("ascii"),
            self._basic(""),
        )
        for value in attempts:
            with self.subTest(authorization=value if value not in (None, "wrong") else value):
                self._assert_challenge(*self._request("GET", "/api/questions-fast", auth=value))
        result, _ = self._json("/api/questions-fast")
        self.assertIn("csrf_token", result)

    def test_correct_basic_get_token_post_success_but_not_individual_identity(self):
        page_code, page_headers, page = self._request("GET", "/")
        self.assertEqual(page_code, 200)
        self.assertIn("text/html", page_headers.get("content-type", ""))
        self.assertIsInstance(page, bytes)
        self.assertIn(b"TOPIK", page)
        listing, listing_headers = self._json("/api/questions-fast")
        self.assertGreaterEqual(len(listing["csrf_token"]), 32)
        self.assertEqual(listing_headers.get("cache-control"), "no-store")
        self.assertEqual(listing["exam_id"], "035-I-B")
        detail, _ = self._json(f"/api/questions/{Q35}?fast=1")
        payload = self._payload(detail)
        before = self._count_reviews()
        status, _, result = self._request(
            "POST", f"/api/questions/{Q35}/review?fast=1",
            payload=payload, headers={"Origin": self.origin,
                                      "X-Review-Token": listing["csrf_token"]},
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["review_status"], "verified")
        self.assertTrue(result["saved"])
        self.assertEqual(self._count_reviews(), before + 1)
        self.assertEqual(result["last_human_review"]["reviewer"], "local_reviewer",
                         "Shared Basic operator access is not a verified personal identity")
        self.assertEqual(result["last_human_review"]["status"], "verified")
        self.assertTrue(result["last_human_review"]["approved"])
        self.assertTrue(any(item["scope"] == "manual_question_review" for item in result["history"]))
        status, _, content = self._request(
            "POST", f"/api/questions/{Q35}/review?fast=1", payload=payload,
            headers={"Origin": self.origin, "X-Review-Token": listing["csrf_token"]},
        )
        self.assertEqual(status, 409, content)
        self.assertEqual(self._count_reviews(), before + 1)

    def test_correct_basic_does_not_bypass_origin_host_or_csrf(self):
        listing, _ = self._json("/api/questions-fast")
        detail, _ = self._json(f"/api/questions/{Q35}")
        payload = self._payload(detail)
        before = self._count_reviews()
        variants = (
            {"Origin": self.origin},
            {"Origin": self.origin, "X-Review-Token": "bad-token"},
            {"Origin": "https://attacker.example", "X-Review-Token": listing["csrf_token"]},
            {"Origin": "null", "X-Review-Token": listing["csrf_token"]},
            {"X-Review-Token": listing["csrf_token"]},
            {"Origin": self.origin, "X-Review-Token": listing["csrf_token"],
             "Host": "attacker.example"},
        )
        for headers in variants:
            with self.subTest(headers=headers):
                status, _, content = self._request(
                    "POST", f"/api/questions/{Q35}/review?fast=1",
                    payload=payload, headers=headers,
                )
                self.assertEqual(status, 403, content)
                self.assertEqual(self._count_reviews(), before)
        status, _, _ = self._request(
            "GET", "/api/questions-fast", headers={"Host": "attacker.example"},
        )
        self.assertEqual(status, 403)

    def test_authenticated_pdf_mp3_range_and_unauthenticated_range(self):
        for route, signature in (
            (f"/media/{Q35}/paper", b"%PDF-"),
            ("/media/035-I-L-001/audio", b"ID3"),
        ):
            with self.subTest(route=route):
                self._assert_challenge(*self._request(
                    "GET", route, auth=None, headers={"Range": "bytes=0-15"},
                ))
                status, headers, content = self._request(
                    "GET", route, headers={"Range": "bytes=0-15"},
                )
                self.assertEqual(status, 206, (headers, content[:100]))
                self.assertEqual(len(content), 16)
                self.assertEqual(headers.get("accept-ranges"), "bytes")
                self.assertRegex(headers.get("content-range", ""), r"^bytes 0-15/\d+$")
                if signature == b"%PDF-":
                    self.assertTrue(content.startswith(signature))
                status, headers, _ = self._request(
                    "GET", route, headers={"Range": "bytes=99999999999-"},
                )
                self.assertEqual(status, 416)
                self.assertRegex(headers.get("content-range", ""), r"^bytes \*/\d+$")

    def test_launch_keys_are_independent_and_do_not_share_authorization(self):
        other_key = secrets.token_urlsafe(32)
        self.assertNotEqual(other_key, self.secret)
        second_port = self._new_server(other_key)
        self._assert_challenge(*self._request(
            "GET", "/api/questions-fast", port=second_port,
        ))
        status, _, data = self._request(
            "GET", "/api/questions-fast", port=second_port,
            auth=self._basic(other_key),
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["exam_id"], "035-I-B")
        self._assert_challenge(*self._request(
            "GET", "/api/questions-fast", auth=self._basic(other_key),
        ))

    def test_new_launch_rejects_prior_launch_csrf_even_with_its_correct_basic(self):
        first_list, _ = self._json("/api/questions-fast")
        second_key = secrets.token_urlsafe(32)
        second_port = self._new_server(second_key)
        second_origin = f"http://127.0.0.1:{second_port}"
        status, _, second_list = self._request(
            "GET", "/api/questions-fast", port=second_port,
            auth=self._basic(second_key),
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(first_list["csrf_token"], second_list["csrf_token"])
        detail, _ = self._json(f"/api/questions/{Q35}")
        before = self._count_reviews()
        for headers in (
            {"Origin": second_origin, "X-Review-Token": first_list["csrf_token"]},
            {"Origin": self.origin, "X-Review-Token": second_list["csrf_token"]},
        ):
            with self.subTest(origin=headers["Origin"]):
                status, _, reply = self._request(
                    "POST", f"/api/questions/{Q35}/review?fast=1",
                    port=second_port, auth=self._basic(second_key),
                    headers=headers, payload=self._payload(detail),
                )
                self.assertEqual(status, 403, reply)
                self.assertEqual(self._count_reviews(), before)

    def test_35_36_exam_query_cannot_cross_read_or_write(self):
        listing35, _ = self._json("/api/questions-fast")
        listing36, _ = self._json("/api/questions-fast?exam_id=036-I-B")
        self.assertEqual(listing35["exam_id"], "035-I-B")
        self.assertEqual(listing36["exam_id"], "036-I-B")
        self.assertTrue(any(item["id"] == Q35 for item in listing35["items"]))
        self.assertTrue(any(item["id"] == Q36 for item in listing36["items"]))
        self.assertFalse(any(item["id"].startswith("036-") for item in listing35["items"]))
        self.assertFalse(any(item["id"].startswith("035-") for item in listing36["items"]))
        detail35, _ = self._json(f"/api/questions/{Q35}")
        detail36, _ = self._json(f"/api/questions/{Q36}?exam_id=036-I-B")
        self.assertEqual(detail35["exam_id"], "035-I-B")
        self.assertEqual(detail36["exam_id"], "036-I-B")
        self.assertNotEqual(detail35["stem"], detail36["stem"])
        for route in (
            f"/api/questions/{Q35}?exam_id=036-I-B",
            f"/api/questions/{Q36}?exam_id=035-I-B",
            f"/media/{Q35}/paper?exam_id=036-I-B",
        ):
            with self.subTest(route=route):
                status, _, _ = self._request("GET", route)
                self.assertEqual(status, 404)
        before35, before36 = self._count_reviews(Q35), self._count_reviews(Q36)
        for path, payload in (
            (f"/api/questions/{Q35}/review?fast=1&exam_id=036-I-B", self._payload(detail35)),
            (f"/api/questions/{Q36}/review?fast=1&exam_id=035-I-B", self._payload(detail36)),
        ):
            with self.subTest(path=path):
                status, _, data = self._request("POST", path, payload=payload, headers={
                    "Origin": self.origin, "X-Review-Token": listing35["csrf_token"],
                })
                self.assertEqual(status, 404, data)
        self.assertEqual(self._count_reviews(Q35), before35)
        self.assertEqual(self._count_reviews(Q36), before36)

    def test_launcher_generates_new_secret_and_prints_username_password_separately(self):
        """Mock only the launcher listener, never bind an operational service."""
        class FakeServer:
            server_port = 34567

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def serve_forever(self):
                pass

        results = []
        for _ in range(2):
            printed = io.StringIO()
            with (patch.object(review_ui, "ReviewStore", return_value=self.store),
                  patch.object(review_ui, "ThreadingHTTPServer",
                               side_effect=lambda *_: FakeServer()) as listener,
                  patch.object(sys, "argv", ["review_ui.py", "--port", "0"]),
                  redirect_stdout(printed)):
                review_ui.main()
            self.assertEqual(listener.call_count, 1)
            output = printed.getvalue()
            user_line = re.search(r"(?im)^.*username\s+operator\s*$", output)
            password_line = re.search(r"(?im)^.*password\s+(\S+)\s*$", output)
            self.assertIsNotNone(user_line, "Launcher must show the operator username")
            self.assertIsNotNone(password_line, "Launcher must show the password separately")
            self.assertNotEqual(user_line.group(0), password_line.group(0))
            credential = password_line.group(1)
            self.assertGreaterEqual(len(credential), 32)
            self.assertNotIn(credential, output.splitlines()[0],
                             "Do not place operator secret in URL")
            results.append(credential)
        self.assertNotEqual(*results, "Every launch needs a fresh random password")

    def test_make_handler_requires_explicit_keyword_only_access_key(self):
        """A security-sensitive handler must not silently invent its credential."""
        parameter = inspect.signature(review_ui.make_handler).parameters["access_key"]
        self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parameter.default, inspect.Parameter.empty,
                      "Explicit operator access_key must be mandatory, never omitted")
        with self.assertRaises(TypeError):
            review_ui.make_handler(self.store)
        for invalid in ("", "short", "x" * 31):
            with self.subTest(length=len(invalid)):
                with self.assertRaises(ValueError):
                    review_ui.make_handler(self.store, access_key=invalid)


if __name__ == "__main__":
    unittest.main()
