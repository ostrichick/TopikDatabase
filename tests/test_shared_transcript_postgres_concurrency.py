"""Opt-in, destructive ONLY within a newly provisioned private PostgreSQL 17 DB.

Prime/operator must start a disposable PG17 cluster, CREATE a *fresh empty*
database and COMMENT ON DATABASE it with TOPIK_SHARED_PG_TEST::<nonce>.
No cluster provisioning or teardown, no writes to operational databases.

  TOPIK_SHARED_PG_TEST_URL=postgresql://test_user@127.0.0.1:55939/topik_shared_pg_test_<nonce>
  TOPIK_SHARED_PG_TEST_MARKER=<matching 16-32 lowercase hex nonce>
  python -m unittest tests.test_shared_transcript_postgres_concurrency -v

ALL guards (URL, port, nonce, marker, empty public schema, PG major version)
run before schema creation or data migration. A previous run's populated DB
is REFUSED, not reused or reset. The frozen SQLite source is mode=ro only.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlencode, quote
import hashlib
import http.client
import json
import os
import re
import threading
import unittest

from scripts import migrate_sqlite_to_postgres
from src import database, review_ui


ROOT = Path(__file__).resolve().parents[1]
FROZEN_SQLITE = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
TEST_URL_ENV = "TOPIK_SHARED_PG_TEST_URL"
TEST_MARKER_ENV = "TOPIK_SHARED_PG_TEST_MARKER"
PREFIX = "topik_shared_pg_test_"
MARKER_PREFIX = "TOPIK_SHARED_PG_TEST::"
NONCE = re.compile(r"[0-9a-f]{16,32}\Z")
PAIRS = ((25, 26), (27, 28), (29, 30))


def validate_test_target(url: str, marker: str, *, operational_url: str = "") -> str:
    """Pure fail-closed validation. Never infer a URL from TOPIK_DATABASE_URL."""
    if not isinstance(url, str) or not isinstance(marker, str):
        raise ValueError("Test URL/marker must be explicit strings")
    if url == operational_url and operational_url:
        raise ValueError("Refusing operational TOPIK_DATABASE_URL")
    if not NONCE.fullmatch(marker):
        raise ValueError("Test marker must be a fresh 16-32-character hex nonce")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid test PostgreSQL URL/port") from exc
    if (parsed.scheme != "postgresql" or parsed.hostname != "127.0.0.1"
            or port is None or not 49152 <= port <= 65535
            or parsed.query not in ("", "sslmode=disable") or bool(parsed.fragment)
            or parsed.path != f"/{PREFIX}{marker}"
            or not parsed.username or parsed.password is not None
            or parsed.netloc.count("@") != 1):
        raise ValueError(
            "Refusing nonlocal, non-ephemeral, nonempty-query or non-nonce test DB URL"
        )
    return url


def assert_pristine_pg17_target(url: str, marker: str) -> None:
    """Read-only checks of the target before *any* mutation."""
    with closing(database.connect_postgres(url, readonly=True)) as conn:
        row = conn.execute(
            "SELECT current_database() AS db, inet_server_port() AS port, "
            "current_schema() AS schema_name, current_setting('server_version_num') AS version"
        ).fetchone()
        target = f"{PREFIX}{marker}"
        if (row["db"] != target or row["schema_name"] != "public"
                or int(row["version"]) // 10000 != 17):
            raise ValueError("Unexpected database/schema or PostgreSQL major version")
        comment = conn.execute(
            "SELECT shobj_description(oid,'pg_database') AS marker "
            "FROM pg_database WHERE datname=current_database()"
        ).fetchone()
        if not comment or comment["marker"] != MARKER_PREFIX + marker:
            raise ValueError("Disposable test database COMMENT marker mismatch")
        # Include all user objects, not just TOPIK tables. Never migrate into a
        # reused or previously initialized schema.
        present = conn.execute(
            "SELECT COUNT(*) AS n FROM pg_class c JOIN pg_namespace s "
            "ON s.oid=c.relnamespace WHERE s.nspname='public' "
            "AND c.relkind IN ('r','p','v','m','S','f')"
        ).fetchone()["n"]
        other = conn.execute(
            "SELECT COUNT(*) AS n FROM pg_proc p JOIN pg_namespace s "
            "ON s.oid=p.pronamespace WHERE s.nspname='public'"
        ).fetchone()["n"]
        if present or other:
            raise ValueError("Target is not brand-new and empty; never reset existing DB")
        # Refuse an unexpected port, including proxied connections.
        parsed = urlsplit(url)
        if row["port"] != parsed.port:
            raise ValueError("Server port is not the explicit isolated listener")


def bounded_url(url: str) -> str:
    """Only append hardcoded resource/time limits after original URL is guarded."""
    return url + ("&" if "?" in url else "?") + urlencode({
        "connect_timeout": "5",
        "options": "-c statement_timeout=15000 -c lock_timeout=8000",
    }, quote_via=quote)


def question_id(number: int) -> str:
    return f"035-I-L-{number:03d}"


def review_payload(detail: dict, *, transcript: str | None = None, suffix: str = "") -> dict:
    return {
        "version": detail["version"],
        "status": "verified",
        "stem": detail["stem"] + suffix,
        "choices": [choice["text"] for choice in detail["choices"]],
        "transcript_text": (detail["transcript"]["text"] if detail["transcript"]
                            else None) if transcript is None else transcript,
        "note": "isolated PG17 full-source concurrency regression",
    }


def source_digest(path: Path) -> str:
    hash_value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hash_value.update(block)
    return hash_value.hexdigest()


class TestTargetSafety(unittest.TestCase):
    def test_valid_private_url_is_accepted_without_connecting(self):
        marker = "0123456789abcdef0123"
        url = f"postgresql://topik_test@127.0.0.1:55939/{PREFIX}{marker}"
        self.assertEqual(validate_test_target(url, marker), url)
        self.assertEqual(validate_test_target(url + "?sslmode=disable", marker),
                         url + "?sslmode=disable")
        bounded = bounded_url(url + "?sslmode=disable")
        self.assertIn("options=-c%20statement_timeout%3D15000%20-c%20lock_timeout%3D8000", bounded)
        self.assertNotIn("+", bounded, "libpq interprets + literally in options")

    def test_unsafe_operational_overrides_and_reused_urls_are_rejected(self):
        n = "0123456789abcdef0123"
        base = f"postgresql://topik_test@127.0.0.1:55939/{PREFIX}{n}"
        for bad in (
            f"postgresql://topik_test@127.0.0.1:5432/{PREFIX}{n}",
            f"postgresql://topik_test@localhost:55939/{PREFIX}{n}",
            f"postgresql://topik_test@10.0.0.1:55939/{PREFIX}{n}",
            f"postgresql://topik_test@127.0.0.1:55939/topik",
            f"postgresql://topik_test@127.0.0.1:55939/{PREFIX}wrong",
            f"postgresql://topik_test@127.0.0.1:55939/{PREFIX}{n}?host=remote",
            f"postgresql://topik_test@127.0.0.1:55939/{PREFIX}{n}?dbname=production",
            f"postgresql://topik_test@127.0.0.1:55939/{PREFIX}{n}?options=-c%20search_path=other",
            f"postgresql://topik_test@127.0.0.1:55939/{PREFIX}{n}?sslmode=prefer",
            f"postgresql://topik_test@127.0.0.1:55939/{PREFIX}{n}?sslmode=disable&host=remote",
            f"postgresql://topik_test:pass@127.0.0.1:55939/{PREFIX}{n}",
            f"postgres://topik_test@127.0.0.1:55939/{PREFIX}{n}",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_test_target(bad, n)
        with self.assertRaises(ValueError):
            validate_test_target(base, n, operational_url=base)
        with self.assertRaises(ValueError):
            validate_test_target(base, "wrong")

    def test_db_guard_is_read_only_and_refuses_wrong_comment_used_schema_or_pg_version(self):
        nonce = "0123456789abcdef0123"
        url = f"postgresql://topik_test@127.0.0.1:55939/{PREFIX}{nonce}"

        class Query:
            def __init__(self, value):
                self.value = value

            def fetchone(self):
                return self.value

        class FakePG:
            def __init__(self, *, version="170011", comment=None, objects=0, functions=0):
                self.version, self.comment = version, comment or MARKER_PREFIX + nonce
                self.objects, self.functions = objects, functions
                self.statements = []

            def execute(self, query):
                self.statements.append(query)
                self_guard = {
                    "shobj_description": {"marker": self.comment},
                    "FROM pg_class": {"n": self.objects},
                    "FROM pg_proc": {"n": self.functions},
                    "current_database()": {
                        "db": PREFIX + nonce, "port": 55939,
                        "schema_name": "public", "version": self.version,
                    },
                }
                for text, result in self_guard.items():
                    if text in query:
                        return Query(result)
                raise AssertionError(f"Unexpected guard SQL: {query}")

            def close(self):
                pass

        for kwargs, accepted in (
            ({}, True),
            ({"version": "160000"}, False),
            ({"comment": "operational"}, False),
            ({"objects": 1}, False),
            ({"functions": 1}, False),
        ):
            with self.subTest(kwargs=kwargs):
                fixture = FakePG(**kwargs)
                with patch.object(database, "connect_postgres", return_value=fixture) as connector:
                    if accepted:
                        assert_pristine_pg17_target(url, nonce)
                    else:
                        with self.assertRaises(ValueError):
                            assert_pristine_pg17_target(url, nonce)
                connector.assert_called_once_with(url, readonly=True)
                self.assertTrue(fixture.statements)
                self.assertTrue(all(query.lstrip().upper().startswith("SELECT")
                                    for query in fixture.statements),
                                "Safety gate may never issue any mutations")


@unittest.skipUnless(
    os.environ.get(TEST_URL_ENV) and os.environ.get(TEST_MARKER_ENV),
    "No explicit isolated PostgreSQL test URL+marker; integration never runs against operational DB",
)
class DisposablePostgres17Concurrency(unittest.TestCase):
    """Each test owns unique 35th questions within ONE never-before-used DB."""

    @classmethod
    def setUpClass(cls):
        cls.marker = os.environ[TEST_MARKER_ENV].strip()
        cls.url = validate_test_target(
            os.environ[TEST_URL_ENV].strip(), cls.marker,
            operational_url=os.environ.get("TOPIK_DATABASE_URL", "").strip(),
        )
        if not FROZEN_SQLITE.is_file():
            raise AssertionError("Immutable 35th SQLite archive not found")
        before_digest = source_digest(FROZEN_SQLITE)
        # Absolutely no write until DB marker + cluster version + empty DB pass.
        assert_pristine_pg17_target(cls.url, cls.marker)
        with patch.dict(os.environ, {database.DATABASE_URL_ENV: cls.url}):
            migration = migrate_sqlite_to_postgres.migrate(FROZEN_SQLITE)
        if migration["status"] != "applied" or not migration["approved_baseline"]:
            raise AssertionError("Frozen 35th dataset failed exact baseline migration")
        if source_digest(FROZEN_SQLITE) != before_digest:
            raise AssertionError("Frozen SQLite source unexpectedly changed")
        cls.store_url = bounded_url(cls.url)
        cls.store = review_ui.ReviewStore(None, root=ROOT, database_url=cls.store_url)
        with closing(database.connect_postgres(cls.store_url, readonly=True)) as conn:
            rows = conn.execute(
                "SELECT q.exam_number,t.source_file_id,t.source_pdf_page,t.dialogue_text "
                "FROM questions q JOIN transcripts t ON t.question_id=q.id "
                "WHERE q.id IN ('035-I-L-025','035-I-L-026','035-I-L-027',"
                "'035-I-L-028','035-I-L-029','035-I-L-030') ORDER BY q.id"
            ).fetchall()
            if len(rows) != 6 or any(
                rows[i][key] != rows[i+1][key]
                for i in (0, 2, 4)
                for key in ("source_file_id", "source_pdf_page", "dialogue_text")
            ):
                raise AssertionError("Expected three frozen shared transcript pairs")

    @classmethod
    def new_store(cls):
        return review_ui.ReviewStore(None, root=ROOT, database_url=cls.store_url)

    @classmethod
    def facts(cls, ids):
        with closing(database.connect_postgres(cls.store_url, readonly=True)) as db:
            output = {}
            for qid in ids:
                output[qid] = {
                    "question": dict(db.execute("SELECT * FROM questions WHERE id=%s", (qid,)).fetchone()),
                    "transcript": dict(db.execute("SELECT * FROM transcripts WHERE question_id=%s", (qid,)).fetchone()),
                    "choices": [dict(r) for r in db.execute("SELECT * FROM choices WHERE question_id=%s ORDER BY number", (qid,))],
                    "audio": dict(db.execute("SELECT * FROM audio_segments WHERE question_id=%s", (qid,)).fetchone()),
                    "history": [dict(r) for r in db.execute(
                        "SELECT * FROM review_records WHERE subject_id=%s ORDER BY id", (qid,))],
                }
            return output

    @staticmethod
    def run_parallel(call_a, call_b):
        gate = threading.Barrier(2)

        def task(operation):
            gate.wait(timeout=10)
            try:
                return ("ok", operation())
            except Exception as exc:
                return ("error", exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(task, fn) for fn in (call_a, call_b)]
            return [future.result(timeout=35) for future in futures]

    def test_10_opposite_direction_pair_writers_have_one_winner_and_cas_conflict(self):
        a, b = map(question_id, PAIRS[0])
        before = self.facts((a, b))
        left = self.store.get_question(a, fast=True)
        right = self.store.get_question(b, fast=True)
        base_a = review_payload(left, transcript=left["transcript"]["text"] + " [PG17-left]")
        base_b = review_payload(right, transcript=right["transcript"]["text"] + " [PG17-right]")
        base_a["shared_transcript"] = {
            "other_question_id": b, "other_version": right["version"],
            "confirm_shared_source": True, "confirm_reset_review": True}
        base_b["shared_transcript"] = {
            "other_question_id": a, "other_version": left["version"],
            "confirm_shared_source": True, "confirm_reset_review": True}
        results = self.run_parallel(
            lambda: self.new_store().save_review(a, base_a, fast_response=True),
            lambda: self.new_store().save_review(b, base_b, fast_response=True),
        )
        successes = [result for kind, result in results if kind == "ok"]
        errors = [result for kind, result in results if kind == "error"]
        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(errors), 1, results)
        self.assertIsInstance(errors[0], review_ui.Conflict,
                              "Contending pair correction must fail CAS, not deadlock/timeout")
        after = self.facts((a, b))
        expected_text = (base_a if successes[0]["id"] == a else base_b)["transcript_text"]
        self.assertEqual(after[a]["transcript"]["dialogue_text"], expected_text)
        self.assertEqual(after[b]["transcript"]["dialogue_text"], expected_text)
        for qid in (a, b):
            self.assertEqual(len(after[qid]["history"]), len(before[qid]["history"]) + 1)
        winner = successes[0]
        loser_id = b if winner["id"] == a else a
        self.assertEqual(after[winner["id"]]["question"]["review_status"], "verified")
        self.assertEqual(after[loser_id]["question"]["review_status"], "needs_manual_review")
        self.assertEqual(winner["shared_transcript_updated"]["id"], loser_id)
        self.assertEqual(after[loser_id]["history"][-1]["scope"],
                         "shared_transcript_source_correction")
        self.assertEqual(after[winner["id"]]["history"][-1]["scope"], "manual_question_review")

        # Also verify the actual HTTP boundary maps the losing CAS to 409,
        # never silently retries it as a second accepted review.
        server = review_ui.ThreadingHTTPServer(
            ("127.0.0.1", 0), review_ui.make_handler(self.new_store())
        )
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            port = server.server_port
            with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=20)) as client:
                client.request("GET", "/api/questions-fast")
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                csrf_token = json.loads(response.read())["csrf_token"]

            latest_a = self.store.get_question(a, fast=True)
            latest_b = self.store.get_question(b, fast=True)
            refreshed_a = review_payload(
                latest_a, transcript=latest_a["transcript"]["text"] + " [HTTP-left]")
            refreshed_b = review_payload(
                latest_b, transcript=latest_b["transcript"]["text"] + " [HTTP-right]")
            refreshed_a["shared_transcript"] = {
                "other_question_id": b, "other_version": latest_b["version"],
                "confirm_shared_source": True, "confirm_reset_review": True}
            refreshed_b["shared_transcript"] = {
                "other_question_id": a, "other_version": latest_a["version"],
                "confirm_shared_source": True, "confirm_reset_review": True}

            def post(qid, data):
                with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=20)) as client:
                    client.request(
                        "POST", f"/api/questions/{qid}/review?fast=1",
                        body=json.dumps(data).encode("utf-8"),
                        headers={"Content-Type": "application/json",
                                 "Origin": f"http://127.0.0.1:{port}",
                                 "X-Review-Token": csrf_token},
                    )
                    response = client.getresponse()
                    return response.status, json.loads(response.read())

            previous = self.facts((a, b))
            http_results = self.run_parallel(
                lambda: post(a, refreshed_a), lambda: post(b, refreshed_b))
            self.assertEqual([kind for kind, _ in http_results], ["ok", "ok"])
            responses = [result for _, result in http_results]
            self.assertEqual(sorted(code for code, _ in responses), [200, 409])
            self.assertTrue(next(body for code, body in responses if code == 200)["saved"])
            self.assertIn("error", next(body for code, body in responses if code == 409))
            new = self.facts((a, b))
            for qid in (a, b):
                self.assertEqual(len(new[qid]["history"]), len(previous[qid]["history"]) + 1)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive(), "Disposable localhost HTTP test server leaked")

    def test_20_concurrent_shared_correction_and_audio_boundary_have_no_deadlock(self):
        a, b = map(question_id, PAIRS[1])
        before = self.facts((a, b))
        initial = self.store.get_question(a, fast=True)
        second = self.store.get_question(b, fast=True)
        correction = review_payload(initial,
                                    transcript=initial["transcript"]["text"] + " [PG17-pair-audio]")
        correction["shared_transcript"] = {
            "other_question_id": b, "other_version": second["version"],
            "confirm_shared_source": True, "confirm_reset_review": True}
        segment = initial["audio_segment"]
        interval = {"version": segment["version"], "start_ms": segment["start_ms"] + 700,
                    "end_ms": segment["end_ms"] + 700, "status": "candidate"}
        results = self.run_parallel(
            lambda: self.new_store().save_review(a, correction, fast_response=True),
            lambda: self.new_store().save_audio_segment(b, interval),
        )
        self.assertEqual([r[0] for r in results], ["ok", "ok"],
                         "Pair review+audio same lock order should not deadlock")
        after = self.facts((a, b))
        for qid in (a, b):
            self.assertEqual(after[qid]["transcript"]["dialogue_text"],
                             correction["transcript_text"])
            self.assertEqual(after[qid]["audio"]["start_ms"], interval["start_ms"])
            self.assertEqual(after[qid]["audio"]["end_ms"], interval["end_ms"])
            self.assertEqual(after[qid]["audio"]["version"], before[qid]["audio"]["version"] + 1)
            self.assertEqual(len(after[qid]["history"]), len(before[qid]["history"]) + 2)
            self.assertEqual(after[qid]["audio"]["source_sha256"], before[qid]["audio"]["source_sha256"])
        self.assertEqual(after[b]["question"]["review_status"], "needs_manual_review")
        self.assertEqual(after[a]["question"]["review_status"], "verified")

    def test_30_forced_midtransaction_error_rolls_back_pair_and_all_history(self):
        a, b = map(question_id, PAIRS[2])
        before = self.facts((a, b))
        first = self.store.get_question(a, fast=True)
        second = self.store.get_question(b, fast=True)
        payload = review_payload(first, transcript=first["transcript"]["text"] + " [MUST ROLLBACK]",
                                 suffix=" [MUST ROLLBACK STEM]")
        payload["shared_transcript"] = {"other_question_id": b, "other_version": second["version"],
                                        "confirm_shared_source": True, "confirm_reset_review": True}
        writer = self.new_store()
        base_connect = writer._connect
        attempts = []

        class Injected:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, args=()):
                if sql.lstrip().startswith("INSERT INTO review_records") and len(args) > 4:
                    if args[4] == "manual_question_review":
                        attempts.append("failure-after-selected-and-sibling-updates")
                        raise RuntimeError("injected failure after shared provenance insert")
                return self.db.execute(sql, args)

            def __getattr__(self, name):
                return getattr(self.db, name)

        def connect(writable=False):
            db = base_connect(writable=writable)
            return Injected(db) if writable else db

        with patch.object(writer, "_connect", side_effect=connect):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                writer.save_review(a, payload, fast_response=True)
        self.assertEqual(len(attempts), 1, "The application must not auto-retry a failed transaction")
        self.assertEqual(self.facts((a, b)), before)

    def test_40_f3_repeatable_read_detail_does_not_mix_old_text_with_new_version(self):
        qid = "035-I-L-005"  # Not an inferred shared pair.
        before = self.store.get_question(qid, fast=True)
        writer = self.new_store()
        reader = self.new_store()
        original_question = reader._question
        payload = review_payload(before, suffix=" [PG17 committed after first snapshot read]")
        payload["choices"][0] += " [committed]"
        calls = []

        def intercept(db, question_id, **kwargs):
            row = original_question(db, question_id, **kwargs)
            calls.append(writer.save_review(qid, payload, fast_response=True))
            return row

        with patch.object(reader, "_question", side_effect=intercept):
            snapshot = reader.get_question(qid, fast=True)
        self.assertEqual(len(calls), 1)
        for field in ("stem", "choices", "version", "review_status", "transcript", "history"):
            self.assertEqual(snapshot[field], before[field], field)
        after = self.store.get_question(qid, fast=True)
        self.assertEqual(after["version"], before["version"] + 1)
        self.assertEqual(after["stem"], payload["stem"])
        self.assertEqual(after["choices"][0]["text"], payload["choices"][0])
        with self.assertRaises(review_ui.Conflict):
            writer.save_review(qid, review_payload(snapshot), fast_response=True)

    def test_50_f3_repeatable_read_bundle_has_one_consistent_revision(self):
        qid = "035-I-L-006"
        original = self.store.get_questions_bundle()["questions"][qid]
        writer = self.new_store()
        reader = self.new_store()
        start = self.store.get_question(qid, fast=True)
        payload = review_payload(start, suffix=" [PG17 commit between bundle reads]")
        payload["choices"][1] += " [committed]"
        base_connect = reader._connect
        calls = []

        class ReaderProxy:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, args=()):
                if "SELECT c.question_id,c.number,c.text" in sql and not calls:
                    calls.append(writer.save_review(qid, payload, fast_response=True))
                return self.db.execute(sql, args)

            def __getattr__(self, name):
                return getattr(self.db, name)

        def connect(writable=False):
            db = base_connect(writable=writable)
            return ReaderProxy(db) if not writable else db

        with patch.object(reader, "_connect", side_effect=connect):
            bundled = reader.get_questions_bundle()["questions"][qid]
        self.assertEqual(len(calls), 1)
        for field in ("stem", "choices", "version", "review_status", "transcript", "history"):
            self.assertEqual(bundled[field], original[field], field)
        fresh = self.store.get_questions_bundle()["questions"][qid]
        self.assertEqual(fresh["stem"], payload["stem"])
        self.assertEqual(fresh["version"], original["version"] + 1)

    def test_60_36th_exam_and_operational_data_are_not_reachable(self):
        before = self.store.get_question("035-I-L-025", fast=True)
        with self.assertRaises(review_ui.ReviewError):
            self.store.for_exam("036-I-B")
        with self.assertRaises(review_ui.NotFound):
            self.store.get_question("036-I-L-025", fast=True)
        with closing(database.connect_postgres(self.store_url, readonly=True)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) AS n FROM questions WHERE id LIKE '036-%'"
            ).fetchone()["n"], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) AS n FROM exams WHERE id='036-I-B'"
            ).fetchone()["n"], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) AS n FROM questions WHERE id LIKE '035-%'"
            ).fetchone()["n"], 70)
        self.assertEqual(self.store.get_question("035-I-L-025", fast=True), before)

    def test_70_audio_verified_requires_matching_candidate_and_atomic_human_evidence(self):
        # 29/30 remains candidate after test_30 forced rollback. Here and in
        # all other tests the evidence is a SIMULATED human declaration on
        # disposable PG, not a claim that anyone listened to the real MP3.
        a, b = (question_id(29), question_id(30))
        before = self.facts((a, b))
        audio = self.store.get_question(a, fast=True)["audio_segment"]
        self.assertEqual(audio["status"], "candidate")
        evidence = {"listened_to_source": True, "checked_start": True,
                    "checked_end": True, "checked_transcript": True,
                    "other_question_confirmed": True,
                    "note": "Simulated person confirmed boundaries and transcript only in test DB"}
        payload = {"version": audio["version"], "start_ms": audio["start_ms"],
                   "end_ms": audio["end_ms"], "status": "verified"}
        with self.assertRaises(review_ui.ReviewError):
            self.new_store().save_audio_segment(a, payload)
        with self.assertRaises(review_ui.Conflict):
            self.new_store().save_audio_segment(a, {**payload,
                "start_ms": payload["start_ms"] + 300, "human_evidence": evidence})
        self.assertEqual(self.facts((a, b)), before)
        ack = self.new_store().save_audio_segment(a, {**payload, "human_evidence": evidence})
        self.assertEqual(ack["audio_segment"]["status"], "verified")
        self.assertEqual(ack["audio_segment"]["human_evidence"]["note"], evidence["note"])
        after = self.facts((a, b))
        for qid in (a, b):
            self.assertEqual(after[qid]["audio"]["status"], "verified")
            self.assertEqual(after[qid]["audio"]["version"], before[qid]["audio"]["version"] + 1)
            self.assertEqual(after[qid]["question"], before[qid]["question"])
            self.assertEqual(after[qid]["transcript"], before[qid]["transcript"])
            last = after[qid]["history"][-1]
            self.assertEqual(last["subject_type"], "audio_segment")
            self.assertEqual(last["scope"], "manual_audio_boundary_35")
            self.assertEqual(__import__('json').loads(last["evidence"])["human_evidence"], evidence)
        with self.assertRaises(review_ui.Conflict):
            self.new_store().save_audio_segment(b, {**payload, "human_evidence": evidence})
        bundle = self.new_store().get_questions_bundle()["questions"]
        self.assertEqual(bundle[a]["audio_segment"]["human_evidence"]["note"], evidence["note"])
        self.assertEqual(bundle[b]["audio_segment"]["human_evidence"]["note"], evidence["note"])

    def test_80_legacy_status_only_cannot_export_or_serve_clip(self):
        """Only mutate the disposable PG17 database, never original/operational data."""
        left, right = (question_id(25), question_id(26))
        with self.new_store()._write_transaction() as db:
            db.execute("UPDATE audio_segments SET status='verified' "
                       "WHERE question_id IN (?,?)", (left, right))
        store = self.new_store()
        for qid in (left, right):
            before = store.get_question(qid, fast=True)["audio_segment"]
            self.assertEqual(before["status"], "verified")
            self.assertIsNone(before["human_evidence"])
            self.assertIsNone(before["clip_url"])
            bundle = store.get_questions_bundle()["questions"][qid]["audio_segment"]
            self.assertIsNone(bundle["human_evidence"])
            self.assertIsNone(bundle["clip_url"])
            with self.assertRaisesRegex(review_ui.ReviewError, "human evidence"):
                store.clip_path(qid)
        with patch("src.audio_35.export_segment") as encoder:
            with self.assertRaisesRegex(review_ui.ReviewError, "human evidence"):
                store.export_audio_clip(left)
        encoder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
