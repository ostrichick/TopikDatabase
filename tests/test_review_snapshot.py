"""Concurrent review snapshots using synthetic media and independently committed writes.

The default tests need no private corpus. SQLite WAL models statement snapshots
for the PostgreSQL branch, and native SQLite tests retain its existing BEGIN
contract. Set TOPIK_SNAPSHOT_TEST_DATABASE_URL to an explicitly disposable local
database named topik_snapshot_test_<suffix> to run real PostgreSQL tests too.
Each live fixture owns a newly generated schema; no production URL is inferred.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from src import database, review_ui


ROOT = Path(__file__).resolve().parents[1]
LIVE_URL = os.environ.get("TOPIK_SNAPSHOT_TEST_DATABASE_URL", "").strip()


class SQLiteStatementSnapshots:
    """Model READ COMMITTED until the caller explicitly requests a snapshot.

    Unlike existing read doubles, SET TRANSACTION actually starts a SQLite WAL
    read transaction. A separate writer can commit while that snapshot is open.
    """

    backend = "postgres"

    def __init__(self, path):
        self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA query_only=ON")
        self.closed = False

    def execute(self, sql, params=()):
        if sql == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY":
            return self.db.execute("BEGIN")
        if "information_schema.tables" in sql:
            if "table_name='audio_segments'" in sql:
                return self.db.execute(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master "
                    "WHERE name='audio_segments') AS present"
                )
            return self.db.execute(
                "SELECT name AS table_name FROM sqlite_master WHERE name LIKE ?", params
            )
        return self.db.execute(sql, params)

    def close(self):
        self.db.close()
        self.closed = True


def disposable_url(url):
    """Fail before connecting if the explicitly supplied test target is unsafe."""
    parsed = urlsplit(url)
    if (parsed.scheme not in ("postgres", "postgresql")
            or parsed.hostname != "127.0.0.1"
            or not parsed.path.startswith("/topik_snapshot_test_")
            or not parsed.path.removeprefix("/topik_snapshot_test_").isalnum()
            or {k for k, _ in parse_qsl(parsed.query)} &
            {"host", "hostaddr", "port", "dbname", "service", "servicefile"}):
        raise ValueError("Snapshot integration tests require an explicit disposable local test DB")
    return parsed


class SnapshotScenarios:
    mode = "simulated_postgres"

    @contextmanager
    def fixture(self, *, session=35, listening=False, reviews=0):
        with tempfile.TemporaryDirectory(prefix="topik-snapshot-") as folder:
            root = Path(folder)
            media_root = root / "topik-past-papers"
            source = media_root / f"{session}th" / "synthetic.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"%PDF-1.4\nsynthetic snapshot fixture\n%%EOF\n")
            exam = f"{session:03d}-I-B"
            name = "listening" if listening else "reading"
            number = 1 if listening else 31
            qid = f"{session:03d}-I-{'L' if listening else 'R'}-{number:03d}"
            section = f"{exam}-{name}"
            group = f"{section}-group"
            db_path = root / "snapshot.sqlite"
            schema = None
            if self.mode == "live_postgres":
                from psycopg import sql
                parsed = disposable_url(LIVE_URL)
                schema = "snapshot_" + uuid.uuid4().hex
                with closing(database.connect_postgres(LIVE_URL, autocommit=True)) as admin:
                    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                self.addCleanup(self.drop_schema, schema)
                query = [(k, v) for k, v in parse_qsl(parsed.query)
                         if k not in ("options", "connect_timeout")]
                query.extend((("options", f"-c search_path={schema} -c statement_timeout=10000 "
                               "-c lock_timeout=5000"), ("connect_timeout", "5")))
                url = urlunsplit(parsed._replace(query=urlencode(query, quote_via=quote)))
                seed = database.PostgresWriteConnection(url)
                schema_text = (ROOT / "db/schema_postgres.sql").read_text(encoding="utf-8")
            else:
                seed = sqlite3.connect(db_path)
                seed.execute("PRAGMA journal_mode=WAL")
                schema_text = (ROOT / "db/schema.sql").read_text(encoding="utf-8")
            with closing(seed):
                core_schema = schema_text.split("CREATE TABLE IF NOT EXISTS ai_audit_source_snapshots")[0]
                if self.mode == "live_postgres":
                    seed._raw.execute(core_schema)
                else:
                    seed.executescript(core_schema)
                seed.execute("INSERT INTO exams(id,session,level,booklet) VALUES(?,?,'I','B')",
                             (exam, session))
                seed.execute("INSERT INTO source_files(id,relative_path,kind,sha256,byte_size) "
                             "VALUES(1,?,'test_paper',?,?)",
                             (source.relative_to(root).as_posix(),
                              hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_size))
                seed.execute("INSERT INTO sections(id,exam_id,name,first_exam_number,last_exam_number) "
                             "VALUES(?,?,?,?,?)", (section, exam, name, number, number))
                seed.execute("INSERT INTO question_groups(id,section_id,first_exam_number,last_exam_number) "
                             "VALUES(?,?,?,?)", (group, section, number, number))
                seed.execute("INSERT INTO questions(id,section_id,group_id,source_file_id,exam_number,"
                             "answer_key_number,source_pdf_page,points,stem,raw_question_text,extraction_origin) "
                             "VALUES(?,?,?,1,?,?,1,2,'old stem','immutable raw','synthetic')",
                             (qid, section, group, number, number))
                seed.executemany("INSERT INTO choices(question_id,number,text) VALUES(?,?,?)",
                                 [(qid, n, f"old choice {n}") for n in range(1, 5)])
                seed.execute("INSERT INTO answers(question_id,choice_number,source_file_id,"
                             "source_pdf_page,preview_and_pdf_agree) VALUES(?,1,1,1,1)", (qid,))
                if listening:
                    seed.execute("INSERT INTO transcripts(question_id,source_file_id,source_pdf_page,"
                                 "dialogue_text) VALUES(?,1,1,'old transcript')", (qid,))
                seed.executemany("INSERT INTO review_records(subject_type,subject_id,status,scope,evidence) "
                                 "VALUES('question',?,'needs_manual_review','fixture','{}')",
                                 [(qid,)] * reviews)
                if session == 36:
                    seed.execute("INSERT INTO import_metadata(key,value) VALUES(?,?)",
                                 ("036-I-B:punctuation:v4-to-v5", "synthetic revision"))
                seed.commit()
            if self.mode == "live_postgres":
                reader = review_ui.ReviewStore(database_url=url, media_root=media_root, exam_id=exam)
                writer = review_ui.ReviewStore(database_url=url, media_root=media_root, exam_id=exam)
            else:
                reader = review_ui.ReviewStore(db_path, root=root, exam_id=exam)
                writer = review_ui.ReviewStore(db_path, root=root, exam_id=exam)
                if self.mode == "simulated_postgres":
                    reader.backend = "postgres"
                    reader._connect = lambda writable=False: SQLiteStatementSnapshots(db_path)
            yield reader, writer, qid, reviews + int(session == 36)

    @staticmethod
    def drop_schema(schema):
        from psycopg import sql
        disposable_url(LIVE_URL)
        with closing(database.connect_postgres(LIVE_URL, autocommit=True)) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    @staticmethod
    def payload(detail, *, changed=False):
        return {"version": detail["version"], "status": "verified",
                "stem": "new stem" if changed else detail["stem"],
                "choices": ([f"new choice {n}" for n in range(1, 5)] if changed
                            else [c["text"] for c in detail["choices"]]),
                "transcript_text": (("new transcript" if changed else detail["transcript"]["text"])
                                    if detail["transcript"] else None),
                "note": "independent writer" if changed else "stale reader"}

    def read_during_commit(self, reader, writer, qid, *, fast=True, bundle=False):
        before = writer.get_question(qid, fast=True)
        calls = []
        if bundle:
            # The first result set has pinned the snapshot; commit before the
            # next component of the bundle is fetched.
            original_connect = reader._connect

            def connect(*args, **kwargs):
                db = original_connect(*args, **kwargs)
                original_execute = db.execute

                def execute(sql, params=()):
                    if "SELECT c.question_id,c.number,c.text" in sql:
                        calls.append(writer.save_review(qid, self.payload(before, changed=True),
                                                        fast_response=True))
                    return original_execute(sql, params)

                # Native sqlite connections have read-only method attributes.
                class Proxy:
                    def execute(self, sql, params=()):
                        return execute(sql, params)

                    def close(self):
                        db.close()

                return Proxy()

            with patch.object(reader, "_connect", side_effect=connect):
                detail = reader.get_questions_bundle()["questions"][qid]
        else:
            original_question = reader._question

            def question(db, question_id, **kwargs):
                old = original_question(db, question_id, **kwargs)
                calls.append(writer.save_review(qid, self.payload(before, changed=True),
                                                fast_response=True))
                return old

            with patch.object(reader, "_question", side_effect=question):
                detail = reader.get_question(qid, fast=fast)
        self.assertEqual(len(calls), 1)
        return before, detail

    def assert_consistent_and_stale(self, before, detail, writer, qid):
        for field in ("stem", "choices", "transcript", "review_status", "version", "history"):
            self.assertEqual(detail[field], before[field], field)
        with self.assertRaises(review_ui.Conflict):
            writer.save_review(qid, self.payload(detail), fast_response=True)
        latest = writer.get_question(qid, fast=True)
        self.assertEqual(latest["stem"], "new stem")
        self.assertEqual([c["text"] for c in latest["choices"]],
                         [f"new choice {n}" for n in range(1, 5)])
        if latest["transcript"]:
            self.assertEqual(latest["transcript"]["text"], "new transcript")
            self.assertEqual(latest["transcript"]["review_status"], "verified")
        self.assertEqual(latest["review_status"], "verified")
        self.assertEqual(latest["version"], before["version"] + 1)
        self.assertEqual(latest["history"][0]["note"], "independent writer")
        self.assertEqual(latest["raw_question_text"], "immutable raw")

    def test_concurrent_detail_snapshot_and_stale_save(self):
        for session in (35, 36):
            for listening in (False, True):
                for reviews in (0, 29, 30, 31):
                    for fast in (False, True):
                        with self.subTest(session=session, listening=listening, reviews=reviews, fast=fast):
                            with self.fixture(session=session, listening=listening, reviews=reviews) as (
                                    reader, writer, qid, version):
                                before, detail = self.read_during_commit(reader, writer, qid, fast=fast)
                                self.assertEqual(before["version"], version)
                                self.assert_consistent_and_stale(before, detail, writer, qid)
                                # Closing the first snapshot allows the next request to see the commit.
                                self.assertEqual(reader.get_question(qid, fast=True)["version"], version + 1)

    def test_bundle_snapshot_remains_consistent(self):
        for session in (35, 36):
            for listening in (False, True):
                with self.subTest(session=session, listening=listening):
                    with self.fixture(session=session, listening=listening) as (reader, writer, qid, _):
                        before, detail = self.read_during_commit(reader, writer, qid, bundle=True)
                        self.assert_consistent_and_stale(before, detail, writer, qid)

    def test_stale_http_save_returns_409_without_extra_history(self):
        with self.fixture(listening=True) as (reader, writer, qid, _):
            before, detail = self.read_during_commit(reader, writer, qid)
            server = review_ui.ThreadingHTTPServer(("127.0.0.1", 0), review_ui.make_handler(writer, access_key=None, allow_unauthenticated_test_fixture=True))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)) as client:
                    client.request("GET", "/api/questions-fast")
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    token = json.loads(response.read())["csrf_token"]
                    client.request("POST", f"/api/questions/{qid}/review?fast=1",
                                   json.dumps(self.payload(detail)),
                                   {"Origin": f"http://127.0.0.1:{server.server_port}",
                                    "Content-Type": "application/json", "X-Review-Token": token})
                    response = client.getresponse()
                    self.assertEqual(response.status, 409, response.read().decode())
                self.assert_consistent_and_stale(before, detail, writer, qid)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

    def test_read_connection_closes_after_success_and_exception(self):
        with self.fixture() as (reader, _, qid, _):
            connections = []
            connect = reader._connect

            def tracked_connect(*args, **kwargs):
                db = connect(*args, **kwargs)
                connections.append(db)
                return db

            with patch.object(reader, "_connect", side_effect=tracked_connect):
                reader.get_question(qid, fast=True)
                with patch.object(reader, "_question", side_effect=RuntimeError("read interrupted")):
                    with self.assertRaisesRegex(RuntimeError, "read interrupted"):
                        reader.get_question(qid, fast=True)
            self.assertEqual(len(connections), 2)
            for connection in connections:
                if self.mode == "live_postgres":
                    self.assertTrue(connection._raw.closed)
                elif self.mode == "simulated_postgres":
                    self.assertTrue(connection.closed)
                else:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        connection.execute("SELECT 1")

    def test_latest_version_can_save_after_conflict(self):
        with self.fixture() as (reader, writer, qid, _):
            before, detail = self.read_during_commit(reader, writer, qid)
            self.assert_consistent_and_stale(before, detail, writer, qid)
            latest = reader.get_question(qid, fast=True)
            payload = self.payload(latest)
            payload["stem"] = "fresh version accepted"
            ack = writer.save_review(qid, payload, fast_response=True)
            self.assertTrue(ack["saved"])
            self.assertEqual(ack["version"], before["version"] + 2)
            self.assertEqual(reader.get_question(qid, fast=True)["stem"], payload["stem"])


class DisposableDatabaseGuardTests(unittest.TestCase):
    def test_operational_remote_and_query_overrides_are_rejected(self):
        for url in ("postgresql://127.0.0.1/topik", "postgresql://remote/topik_snapshot_test_fixture",
                    "postgresql://127.0.0.1/topik_snapshot_test_fixture?dbname=topik",
                    "postgresql://127.0.0.1/topik_snapshot_test_fixture?host=remote",
                    "postgresql://127.0.0.1/topik_snapshot_test_fixture?service=production"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                disposable_url(url)


class SimulatedPostgresSnapshotTests(SnapshotScenarios, unittest.TestCase):
    pass


class NativeSQLiteSnapshotTests(SnapshotScenarios, unittest.TestCase):
    mode = "sqlite"


@unittest.skipUnless(LIVE_URL, "Explicit disposable PostgreSQL snapshot test URL is not configured")
class LivePostgresSnapshotTests(SnapshotScenarios, unittest.TestCase):
    mode = "live_postgres"


if __name__ == "__main__":
    unittest.main()
