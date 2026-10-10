"""Local review screen for the 35th TOPIK I pilot database.

Run from the project root: py -3 src/review_ui.py
Open the local URL printed in the terminal (port 8765 or an available fallback).
After Stage 10 the operational reviewer requires TOPIK_DATABASE_URL and uses
central PostgreSQL for human-review state. Explicit non-canonical SQLite paths
remain available only for tests and offline legacy fixtures. PostgreSQL uses
optimistic version checks plus PostgreSQL row locks. AI audit writes remain
disabled; stage 8 keeps clip files device-local while PostgreSQL stores only
their canonical logical path and checksum.
"""

from __future__ import annotations

import argparse
import base64
from collections import defaultdict
import errno
import gzip
import hashlib
import inspect
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import (
    DatabaseConfigError,
    DatabaseOperationError,
    PostgresReadConnection,
    PostgresWriteConnection,
    get_database_url,
    get_media_root,
)
from src.sqlite_archive import assert_sqlite_write_allowed
from src.extraction_rules import normalize_punctuation_spacing_v3


ROOT = PROJECT_ROOT
DB_PATH = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
HTML_PATH = Path(__file__).with_name("review_ui.html")
THIRD_PASS_AUDIT_PATH = ROOT / "topik-past-papers" / "derived" / "audit35-third-pass-20261008" / "ai-audit-35-third-pass-gpt6.json"
DEFAULT_EXAM_ID = "035-I-B"
EXAM_ID = re.compile(r"^(\d{3})-(I|II)-([A-Z])$")
QUESTION_ID = re.compile(r"^\d{3}-(?:I|II)-[LR]-\d{3}$")
STATUSES = ("needs_manual_review", "verified", "rejected")
MAX_POST_BYTES = 64 * 1024
SHARED_AUDIO = {25: (25, 26), 26: (25, 26), 27: (27, 28), 28: (27, 28),
                29: (29, 30), 30: (29, 30)}
AI_AUDIT_TABLES = frozenset({
    "ai_audit_source_snapshots", "ai_audit_runs", "ai_audit_passes",
    "ai_audit_results", "ai_audit_findings", "ai_audit_finding_occurrences",
})


class ReviewError(ValueError):
    """An invalid review operation or user input."""


class NotFound(ReviewError):
    """The requested question or media does not exist."""


class Conflict(ReviewError):
    """The question changed since the browser last loaded it."""


class ReviewStore:
    def __init__(
        self,
        db_path: Path | None = None,
        root: Path = ROOT,
        *,
        database_url: str | None = None,
        media_root: Path | None = None,
        exam_id: str = DEFAULT_EXAM_ID,
    ):
        if not isinstance(exam_id, str) or not EXAM_ID.fullmatch(exam_id):
            raise ReviewError("Invalid exam ID")
        self.exam_id = exam_id
        self.question_prefix = exam_id.rsplit("-", 1)[0] + "-"
        self.root = Path(root).resolve()
        explicit_sqlite = db_path is not None
        resolved_url = database_url.strip() if isinstance(database_url, str) and database_url.strip() else None
        if not explicit_sqlite and resolved_url is None:
            resolved_url = get_database_url()
        if not explicit_sqlite and resolved_url is None:
            raise DatabaseConfigError(
                "Stage 10 requires TOPIK_DATABASE_URL for operational review; "
                "implicit SQLite fallback is disabled"
            )
        self.backend = "sqlite" if explicit_sqlite else "postgres"
        self.database_url = resolved_url
        self.db_path = Path(db_path or DB_PATH).resolve()
        if media_root is not None:
            self.media_root = Path(media_root).expanduser().resolve()
        elif explicit_sqlite:
            self.media_root = (self.root / "topik-past-papers").resolve()
        else:
            self.media_root = get_media_root()
        self.source_root = (self.media_root / f"{int(EXAM_ID.fullmatch(exam_id).group(1))}th").resolve()
        self._has_ai_runs_cache: bool | None = None
        self._has_audio_segments_cache: bool | None = None
        self._audio_asset_cache: dict[str, dict] = {}
        self._list_questions_cache: dict | None = None
        self._ai_summaries_cache: tuple[bool, dict] | None = None
        self._ai_summaries_loading: bool = False
        self._ai_summaries_error: bool = False
        self._ai_summaries_checked_at: float = 0.0
        self._ai_summary_lock = threading.Lock()
        self._ai_summary_generation = 0
        self._exam_stores: dict[str, ReviewStore] = {}
        self._exam_stores_lock = threading.Lock()
        if self.backend == "sqlite" and not self.db_path.is_file():
            raise FileNotFoundError(f"Pilot database not found: {self.db_path}. Run py -3 src/pilot_35.py first.")
        with closing(self._connect()) as db:
            exam = db.execute(
                "SELECT session,level,booklet FROM exams WHERE id=?", (self.exam_id,)
            ).fetchone()
            match = EXAM_ID.fullmatch(self.exam_id)
            if exam is None or (exam["session"], exam["level"], exam["booklet"]) != (
                int(match.group(1)), match.group(2), match.group(3)
            ):
                raise ReviewError(f"Requested exam {self.exam_id} is unavailable or inconsistent")

    def for_exam(self, exam_id: str) -> ReviewStore:
        """Resolve an explicitly selected exam without changing this store's default."""
        if exam_id == self.exam_id:
            return self
        if not isinstance(exam_id, str) or not EXAM_ID.fullmatch(exam_id):
            raise ReviewError("Invalid exam ID")
        with self._exam_stores_lock:
            if exam_id not in self._exam_stores:
                self._exam_stores[exam_id] = ReviewStore(
                    self.db_path if self.backend == "sqlite" else None,
                    root=self.root, database_url=self.database_url,
                    media_root=self.media_root, exam_id=exam_id,
                )
            return self._exam_stores[exam_id]

    def _media_url(self, question_id: str, kind: str, *, page: int | None = None) -> str:
        suffix = "" if self.exam_id == DEFAULT_EXAM_ID else f"?exam_id={self.exam_id}"
        fragment = f"#page={page}" if page is not None else ""
        return f"/media/{question_id}/{kind}{suffix}{fragment}"

    def _connect(self, writable: bool = False):
        if self.backend == "postgres":
            if not self.database_url:
                raise DatabaseConfigError("TOPIK_DATABASE_URL is not configured")
            return PostgresWriteConnection(self.database_url) if writable else PostgresReadConnection(self.database_url)
        if writable:
            assert_sqlite_write_allowed(self.db_path)
        connection = sqlite3.connect(self.db_path.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                                     uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _write_transaction(self, *, before_rollback=None, connection=None):
        db = connection if connection is not None else self._connect(writable=True)
        try:
            if self.backend == "sqlite":
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            if before_rollback is not None:
                try:
                    before_rollback()
                except BaseException:
                    # Cleanup is best-effort and must not hide the database
                    # failure that caused the rollback.
                    pass
            try:
                db.rollback()
            except BaseException:
                # Preserve the original failure; the connection is closed below
                # and no automatic retry is attempted.
                pass
            raise
        finally:
            if connection is None:
                db.close()

    @contextmanager
    def _review_write_transaction(self, question_id: str):
        """Verify original media before locking, reusing one PostgreSQL session.

        A rollback after source validation ends its read transaction before the
        locked write begins. Version checks still run *after* acquiring the
        question lock, so migrations or other reviews racing with preflight
        cannot slip through the optimistic concurrency guard.
        """
        if self.backend == "postgres":
            started = time.perf_counter()
            connected_at = preflight_at = None
            try:
                with closing(self._connect(writable=True)) as db:
                    connected_at = time.perf_counter()
                    try:
                        self._verify_review_media(db, question_id)
                    except BaseException:
                        db.rollback()
                        raise
                    # A new READ COMMITTED transaction starts for FOR UPDATE.
                    db.rollback()
                    preflight_at = time.perf_counter()
                    with self._write_transaction(connection=db) as transaction:
                        yield transaction
            finally:
                if os.environ.get("TOPIK_REVIEW_TIMING") == "1":
                    finished_at = time.perf_counter()
                    connect_ms = (connected_at - started) * 1000 if connected_at else -1
                    preflight_ms = ((preflight_at - connected_at) * 1000
                                    if preflight_at and connected_at else -1)
                    write_ms = ((finished_at - preflight_at) * 1000
                                if preflight_at else -1)
                    print(f"review_pg_timing connect_ms={connect_ms:.1f} "
                          f"preflight_ms={preflight_ms:.1f} write_ms={write_ms:.1f} "
                          f"total_ms={(finished_at - started) * 1000:.1f}", file=sys.stderr)
        else:
            with closing(self._connect()) as preflight:
                self._verify_review_media(preflight, question_id)
            with self._write_transaction() as transaction:
                yield transaction

    def _verify_review_media(self, db, question_id: str) -> None:
        if self.backend == "postgres":
            # Fetch all 2-3 immutable source metadata rows in one SQL roundtrip.
            # Keep full file SHA-256 verification, never trust a cached hash.
            if not isinstance(question_id, str) or not QUESTION_ID.fullmatch(question_id) \
                    or not question_id.startswith(self.question_prefix):
                raise NotFound("Unknown question")
            row = db.execute(
                "SELECT sec.name AS section, "
                "q.source_file_id AS paper_id, paper.relative_path AS paper_path, "
                "paper.sha256 AS paper_hash, paper.byte_size AS paper_size, "
                "a.source_file_id AS answer_id, ans.relative_path AS answer_path, "
                "ans.sha256 AS answer_hash, ans.byte_size AS answer_size, "
                "t.source_file_id AS transcript_id, trans.relative_path AS transcript_path, "
                "trans.sha256 AS transcript_hash, trans.byte_size AS transcript_size "
                "FROM questions q JOIN sections sec ON sec.id=q.section_id "
                "JOIN answers a ON a.question_id=q.id "
                "LEFT JOIN transcripts t ON t.question_id=q.id "
                "LEFT JOIN source_files paper ON paper.id=q.source_file_id "
                "LEFT JOIN source_files ans ON ans.id=a.source_file_id "
                "LEFT JOIN source_files trans ON trans.id=t.source_file_id "
                "WHERE q.id=? AND sec.exam_id=?",
                (question_id, self.exam_id),
            ).fetchone()
            if row is None:
                raise NotFound("Unknown question")
            kinds = (("paper", "answer", "transcript") if row["section"] == "listening"
                     else ("paper", "answer"))
            for kind in kinds:
                if row[f"{kind}_id"] is None:
                    raise NotFound("This question has no such source")
                if row[f"{kind}_path"] is None:
                    raise NotFound("Source does not exist")
                self._verify_media_source(
                    row[f"{kind}_path"], row[f"{kind}_hash"], row[f"{kind}_size"], kind,
                )
            return
        question = self._question(db, question_id)
        kinds = (("paper", "answer", "transcript") if question["section"] == "listening"
                 else ("paper", "answer"))
        for kind in kinds:
            self._media_path_with_connection(db, question, kind)

    def _lock_question(self, db, question_id: str):
        if self.backend == "postgres":
            # Lock the question in the same query that loads its editor fields.
            # OF q avoids locking nullable LEFT JOIN rows in PostgreSQL.
            return self._question(db, question_id, for_update=True)
        return self._question(db, question_id)

    def _locked_audio_rows(self, db, qids: list[str]) -> dict[str, object]:
        ordered = sorted(qids)
        placeholders = ",".join("?" for _ in ordered)
        if self.backend == "postgres":
            locked = db.execute(
                f"SELECT id FROM questions WHERE id IN ({placeholders}) ORDER BY id FOR UPDATE",
                ordered,
            ).fetchall()
            if len(locked) != len(ordered):
                raise NotFound("Unknown shared audio question")
            sql = (
                f"SELECT * FROM audio_segments WHERE question_id IN ({placeholders}) "
                "ORDER BY question_id FOR UPDATE"
            )
        else:
            sql = f"SELECT * FROM audio_segments WHERE question_id IN ({placeholders}) ORDER BY question_id"
        return {item["question_id"]: item for item in db.execute(sql, ordered)}

    def _question(self, db: sqlite3.Connection, question_id: str, *, for_update: bool = False) -> sqlite3.Row:
        if (not isinstance(question_id, str) or not QUESTION_ID.fullmatch(question_id)
                or not question_id.startswith(self.question_prefix)):
            raise NotFound("Unknown question")
        row = db.execute(
            "SELECT q.*, s.name AS section, g.instruction, g.passage_text, "
            "g.first_exam_number, g.last_exam_number, a.choice_number, "
            "a.source_file_id AS answer_file_id, a.source_pdf_page AS answer_pdf_page "
            "FROM questions q JOIN sections s ON s.id=q.section_id "
            "LEFT JOIN question_groups g ON g.id=q.group_id "
            "JOIN answers a ON a.question_id=q.id WHERE q.id=? AND s.exam_id=?"
            + (" FOR UPDATE OF q" if for_update and self.backend == "postgres" else ""),
            (question_id, self.exam_id),
        ).fetchone()
        if row is None:
            raise NotFound("Unknown question")
        return row

    def _punctuation_revision(self, db) -> int:
        """Invalidate 36th review forms opened before the v4 -> v5 migration.

        The migration intentionally does not fabricate human review records.
        Its immutable metadata marker is a separate one-time source revision.
        """
        if self.exam_id != "036-I-B":
            return 0
        return db.execute(
            "SELECT COUNT(*) AS revision FROM import_metadata WHERE key IN (?,?)",
            ("036-I-B:punctuation:v4-to-v5", "036-I-B:punctuation:v5-to-v6"),
        ).fetchone()["revision"]

    def _version(self, db, question_id: str) -> int:
        if self.backend == "postgres" and self.exam_id == "036-I-B":
            # One database roundtrip for both human review count and additive
            # source punctuation revisions (v4→v5, then v5→v6).
            row = db.execute(
                "SELECT COUNT(*) + (SELECT COUNT(*) FROM import_metadata "
                "WHERE key IN (?,?)) AS review_count "
                "FROM review_records WHERE subject_type='question' AND subject_id=?",
                ("036-I-B:punctuation:v4-to-v5",
                 "036-I-B:punctuation:v5-to-v6", question_id),
            ).fetchone()
            return row["review_count"]
        row = db.execute(
            "SELECT COUNT(*) AS review_count FROM review_records WHERE subject_type='question' "
            "AND subject_id=?",
            (question_id,)
        ).fetchone()
        return row["review_count"] + self._punctuation_revision(db)

    @staticmethod
    def _add_punctuation_preview(detail: dict) -> dict:
        """Derived editor typography; never mutate canonical 35th/36th DB text."""
        if detail.get("exam_id") not in ("035-I-B", "036-I-B"):
            return detail
        normal = normalize_punctuation_spacing_v3
        detail["stem_display"] = normal(detail["stem"])
        detail["group"]["instruction_display"] = normal(detail["group"]["instruction"])
        detail["group"]["passage_text_display"] = normal(detail["group"]["passage_text"])
        for choice in detail["choices"]:
            choice["display_text"] = normal(choice["text"])
        if detail.get("transcript"):
            detail["transcript"]["display_text"] = normal(detail["transcript"]["text"])
        return detail

    @staticmethod
    def _audit_timestamp_verified(value: object) -> bool:
        """Only an explicit, timezone-aware audit timestamp is comparable."""
        if not isinstance(value, str) or not value.strip():
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None

    def list_questions(self) -> dict:
        if self.backend == "postgres":
            # Keep this legacy full-list endpoint compatible, but never run
            # N+1 subject status queries or build detailed summaries here.
            audit_data = self.list_ai_audit_summary()
            audit_items = {item["id"]: item["ai_audit"] for item in audit_data["items"]}
            with closing(self._connect()) as db:
                items = [dict(row) for row in db.execute(
                    "SELECT q.id,q.exam_number AS number,s.name AS section,"
                    "q.review_status AS status,q.requires_image "
                    "FROM questions q JOIN sections s ON s.id=q.section_id "
                    "WHERE s.exam_id=? ORDER BY q.exam_number", (self.exam_id,)
                ).fetchall()]
            for item in items:
                if item["id"] in audit_items:
                    item["ai_audit"] = audit_items[item["id"]]
            counts = {status: sum(item["status"] == status for item in items) for status in STATUSES}
            counts["total"] = len(items)
            return {
                "exam_id": self.exam_id, "items": items, "counts": counts,
                "ai_audit_available": audit_data["ai_audit_available"],
                "read_only": False, "database_backend": "postgres",
                "capabilities": {"review_write": True,
                                 "audio_segment_write": self.exam_id == DEFAULT_EXAM_ID,
                                 "clip_export": self.exam_id == DEFAULT_EXAM_ID,
                                 "ai_audit_write": False},
            }
        if (self.exam_id == DEFAULT_EXAM_ID and self._ai_summaries_checked_at
                and time.monotonic() - self._ai_summaries_checked_at > 60):
            # External audit jobs may update summaries without a review write.
            self._ai_summaries_cache = None
            self._list_questions_cache = None
            self._ai_summaries_checked_at = 0.0
        if self._list_questions_cache is not None:
            return self._list_questions_cache
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT q.id, q.exam_number AS number, s.name AS section, "
                "q.review_status AS status, q.requires_image "
                "FROM questions q JOIN sections s ON s.id=q.section_id "
                "WHERE s.exam_id=? ORDER BY q.exam_number", (self.exam_id,)
            ).fetchall()
            items = [dict(row) for row in rows]
            audit_target = self.database_url if self.backend == "postgres" else db
            if self.exam_id != DEFAULT_EXAM_ID:
                ai_available, ai_summaries = False, {}
            elif self._ai_summaries_cache is not None:
                ai_available, ai_summaries = self._ai_summaries_cache
            elif self.backend == "sqlite":
                self._ai_summaries_cache = self._ai_audit_summaries(audit_target)
                self._ai_summaries_checked_at = time.monotonic()
                ai_available, ai_summaries = self._ai_summaries_cache
            else:
                ai_available = self._has_ai_audit_tables(db)
                ai_summaries = {}
                if not self._ai_summaries_loading and not (
                    self._ai_summaries_error and time.monotonic() - self._ai_summaries_checked_at < 10
                ):
                    self._ai_summaries_loading = True
                    def _preload():
                        try:
                            self._ai_summaries_cache = self._ai_audit_summaries(self.database_url)
                            self._ai_summaries_error = False
                        except Exception:
                            # AI audit failure must not interrupt human review.
                            self._ai_summaries_error = True
                        finally:
                            self._ai_summaries_checked_at = time.monotonic()
                            self._list_questions_cache = None
                            self._ai_summaries_loading = False
                    import threading
                    threading.Thread(target=_preload, daemon=True).start()
            if ai_available and not self._ai_summaries_loading and not self._ai_summaries_error:
                try:
                    from src.ai_audit_35 import audit_subject_ids
                except ImportError:
                    execution_subjects = None
                else:
                    execution_subjects = audit_subject_ids(audit_target)

                audit_exec_db = None
                if self.backend == "postgres":
                    try:
                        from src.database import PostgresAuditConnection
                        audit_exec_db = PostgresAuditConnection(self.database_url, readonly=True)
                    except Exception:
                        audit_exec_db = audit_target
                else:
                    audit_exec_db = db

                exec_cache = {}
                try:
                    for item in items:
                        qid = item["id"]
                        is_scoped = execution_subjects is None or qid in execution_subjects
                        summary = ai_summaries.get(qid)
                        audit = self._ai_audit_list_summary(summary) if summary else {}
                        run_id = summary.get("run_id") if summary else None
                        if is_scoped:
                            # Execution status is subject-scoped, even when
                            # multiple questions belong to the same audit run.
                            # None is a valid run selector (latest pending run).
                            cache_key = (run_id, qid)
                            if cache_key not in exec_cache:
                                exec_cache[cache_key] = self._ai_audit_execution(audit_exec_db, subject_id=qid, run_id=run_id)
                            execution = exec_cache[cache_key]
                        else:
                            execution = None
                        execution_summary = self._ai_audit_execution_list_summary(execution)
                        if execution_summary:
                            audit.update(execution_summary)
                        if audit:
                            item["ai_audit"] = audit
                finally:
                    if self.backend == "postgres" and hasattr(audit_exec_db, "close"):
                        audit_exec_db.close()
            counts = {status: sum(item["status"] == status for item in items) for status in STATUSES}
            counts["total"] = len(items)
            result = {
                "exam_id": self.exam_id,
                "items": items,
                "counts": counts,
                "ai_audit_available": ai_available,
                "read_only": False,
                "database_backend": self.backend,
                "capabilities": {
                    "review_write": True,
                    "audio_segment_write": self.exam_id == DEFAULT_EXAM_ID,
                    "clip_export": self.exam_id == DEFAULT_EXAM_ID,
                    "ai_audit_write": False,
                },
            }
            self._list_questions_cache = result
            return result

    def list_ai_audit_summary(self) -> dict:
        """Optional AI metadata only; fast-list review status remains authoritative."""
        if self.exam_id != DEFAULT_EXAM_ID:
            return {"exam_id": self.exam_id, "state": "unavailable",
                    "ai_audit_available": False, "items": []}
        if self.backend == "sqlite":
            listing = self.list_questions()
            if self._ai_summaries_loading:
                status = "loading"
            elif self._ai_summaries_error:
                status = "error"
            else:
                status = "ready" if listing["ai_audit_available"] else "unavailable"
            items = ([{"id": item["id"], "ai_audit": item["ai_audit"]}
                      for item in listing["items"] if "ai_audit" in item]
                     if status == "ready" else [])
        else:
            self._ensure_ai_summary_started()
            with self._ai_summary_lock:
                cached = self._ai_summaries_cache
                loading = self._ai_summaries_loading
                errored = self._ai_summaries_error
            if cached is not None:
                status = "ready" if cached[0] else "unavailable"
                items = ([{"id": qid, "ai_audit": audit} for qid, audit in cached[1].items()]
                         if cached[0] else [])
            else:
                status = "loading" if loading else "error" if errored else "unavailable"
                items = []
        return {
            "exam_id": self.exam_id,
            "state": status,
            "ai_audit_available": status == "ready",
            "items": items,
        }

    def _compute_ai_list_summary(self) -> tuple[bool, dict]:
        """Compute audit-only rows without fetching human review state."""
        from src.ai_audit_list_summary import summarize_list_bulk
        from src.database import PostgresAuditConnection
        with closing(PostgresAuditConnection(self.database_url, readonly=True)) as db:
            # Let bulk reader establish its repeatable-read snapshot before
            # issuing any table-discovery SQL on the connection.
            data = summarize_list_bulk(db)
            return self._has_ai_audit_tables(db), data

    def _ensure_ai_summary_started(self) -> None:
        """Single-flight refresh; a slow or failing worker never blocks fast lists."""
        if self.backend != "postgres" or self.exam_id != DEFAULT_EXAM_ID:
            return
        now = time.monotonic()
        with self._ai_summary_lock:
            if self._ai_summaries_loading:
                return
            fresh = self._ai_summaries_cache is not None and now - self._ai_summaries_checked_at < 60
            cooldown = self._ai_summaries_error and now - self._ai_summaries_checked_at < 10
            if fresh or cooldown:
                return
            self._ai_summaries_loading = True
            self._ai_summary_generation += 1
            generation = self._ai_summary_generation

        def compute():
            started = time.monotonic()
            try:
                result = self._compute_ai_list_summary()
            except Exception as exc:
                # Preserve last valid snapshot. Error state is bounded by the
                # cooldown and carries no source data, credentials or raw SQL.
                logging.warning("AI list summary unavailable (type=%s elapsed_ms=%d)",
                                type(exc).__name__, int((time.monotonic() - started) * 1000))
                with self._ai_summary_lock:
                    if generation == self._ai_summary_generation:
                        self._ai_summaries_error = True
                        self._ai_summaries_checked_at = time.monotonic()
                        self._ai_summaries_loading = False
                return
            with self._ai_summary_lock:
                if generation == self._ai_summary_generation:
                    self._ai_summaries_cache = result
                    self._ai_summaries_error = False
                    self._ai_summaries_checked_at = time.monotonic()
                    self._ai_summaries_loading = False

        threading.Thread(target=compute, name="topik-ai-summary", daemon=True).start()

    @staticmethod
    def _question_history_item(record) -> dict:
        """A bounded, factual review event; never synthesize approval from status."""
        item = {key: record[key] for key in ("id", "status", "scope", "reviewer", "reviewed_at")}
        try:
            parsed = json.loads(record["evidence"])
            item["note"] = parsed.get("note", "") if isinstance(parsed, dict) else ""
        except (TypeError, ValueError):
            item["note"] = ""
        return item

    @classmethod
    def _question_human_evidence(cls, status: str, latest, manual) -> tuple[dict | None, dict]:
        """Separate stored decision, latest manual event, and unknown actor identity.

        Freshness is established by the latest *question* review record, not
        by a matching status or a timestamp alone. Older approvals can remain
        in append-only history but must never be represented as current.
        """
        if manual is None:
            last_human = None
            reason = "missing_manual_review" if status == "verified" else "status_not_verified"
        else:
            same_event = latest is not None and manual["id"] == latest["id"]
            same_status = manual["status"] == status
            has_reviewer = isinstance(manual["reviewer"], str) and bool(manual["reviewer"].strip())
            has_timestamp = cls._audit_timestamp_verified(manual["reviewed_at"])
            current = same_event and same_status and has_reviewer and has_timestamp
            last_human = {
                "id": manual["id"], "record_id": manual["id"],
                "status": manual["status"], "reviewer": manual["reviewer"],
                "reviewed_at": manual["reviewed_at"],
                "is_current": bool(current),
                "approved": bool(current and status == "verified"),
                # The local reviewer string is a declaration, not an authenticated person.
                "identity_verified": False,
            }
            if status != "verified":
                reason = "status_not_verified"
            elif not same_event:
                reason = "superseded_manual_review"
            elif not same_status:
                reason = "manual_status_mismatch"
            elif not has_reviewer or not has_timestamp:
                reason = "incomplete_manual_review"
            else:
                reason = "latest_manual_review"
        approved = bool(last_human and last_human["approved"])
        evidence = {
            "approval_state": ("current_manual_approval" if approved else
                               "verified_without_current_manual_approval" if status == "verified" else
                               "not_verified"),
            "reason": reason,
            "approved": approved,
            "identity_verified": False,
        }
        return last_human, evidence

    def list_questions_fast(self) -> dict:
        """Lightweight list with current human-review evidence in one bulk read."""
        with closing(self._connect()) as db:
            items = [dict(row) for row in db.execute(
                "WITH ranked_reviews AS ("
                " SELECT id, subject_id, status, reviewer, scope, reviewed_at,"
                " COUNT(*) OVER (PARTITION BY subject_id) AS review_version,"
                " ROW_NUMBER() OVER (PARTITION BY subject_id ORDER BY id DESC) AS latest_rank,"
                " ROW_NUMBER() OVER (PARTITION BY subject_id, scope ORDER BY id DESC) AS scope_rank"
                # Rank only reviews belonging to the requested exam. Ranking
                # every historical exam (even if the outer list is scoped)
                # forces unnecessary window sorts as the shared DB grows.
                " FROM review_records WHERE subject_type='question'"
                " AND subject_id IN (SELECT q2.id FROM questions q2"
                " JOIN sections s2 ON s2.id=q2.section_id WHERE s2.exam_id=?)"
                ") "
                "SELECT q.id, q.exam_number AS number, s.name AS section,"
                " q.review_status AS status, q.requires_image,"
                " COALESCE(latest.review_version, 0) AS review_version,"
                " latest.id AS latest_review_id, manual.id AS manual_review_id,"
                " manual.status AS manual_review_status, manual.reviewer AS manual_reviewer,"
                " manual.reviewed_at AS manual_reviewed_at"
                " FROM questions q JOIN sections s ON s.id=q.section_id"
                " LEFT JOIN ranked_reviews latest ON latest.subject_id=q.id AND latest.latest_rank=1"
                " LEFT JOIN ranked_reviews manual ON manual.subject_id=q.id"
                " AND manual.scope='manual_question_review' AND manual.scope_rank=1"
                " WHERE s.exam_id=? ORDER BY q.exam_number",
                (self.exam_id, self.exam_id),
            ).fetchall()]
            punctuation_revision = self._punctuation_revision(db)
        if punctuation_revision:
            for item in items:
                item["review_version"] += punctuation_revision
        for item in items:
            record_id = item.pop("manual_review_id")
            record_status = item.pop("manual_review_status")
            reviewer = item.pop("manual_reviewer")
            reviewed_at = item.pop("manual_reviewed_at")
            latest_id = item.pop("latest_review_id")
            manual = ({"id": record_id, "status": record_status, "reviewer": reviewer,
                       "reviewed_at": reviewed_at} if record_id is not None else None)
            latest = {"id": latest_id} if latest_id is not None else None
            item["last_human_review"], item["human_review_evidence"] = (
                self._question_human_evidence(item["status"], latest, manual))
        counts = {status: sum(item["status"] == status for item in items) for status in STATUSES}
        counts["total"] = len(items)
        return {"exam_id": self.exam_id, "items": items, "counts": counts, "ai_audit_available": False,
                "read_only": False, "database_backend": self.backend,
                "capabilities": {"review_write": True,
                                 "audio_segment_write": self.exam_id == DEFAULT_EXAM_ID,
                                 "clip_export": self.exam_id == DEFAULT_EXAM_ID,
                                 "ai_audit_write": False}}

    @staticmethod
    def _has_ai_audit_tables(db) -> bool:
        if isinstance(db, str) and db.strip().lower().startswith(("postgresql://", "postgres://")):
            try:
                from src.ai_audit_35 import audit_tables_available
            except ImportError:
                return False
            try:
                return audit_tables_available(db)
            except (DatabaseConfigError, DatabaseOperationError):
                return False
        if getattr(db, "backend", None) == "postgres":
            try:
                rows = db.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=current_schema() AND table_name LIKE ?",
                    ("ai_audit_%",),
                ).fetchall()
                existing = {row[0] if isinstance(row, tuple) else row["table_name"] for row in rows}
                return AI_AUDIT_TABLES.issubset(existing)
            except Exception:
                return False
        existing = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ai_audit_%'"
            )
        }
        return AI_AUDIT_TABLES.issubset(existing)

    @classmethod
    def _ai_audit_summaries(cls, db) -> tuple[bool, dict]:
        if not cls._has_ai_audit_tables(db):
            return False, {}
        try:
            from src.ai_audit_35 import summarize_all_questions
        except ImportError:
            # A legacy checkout can encounter a DB upgraded by a newer audit
            # tool. Keep the human review screen usable until code catches up.
            return False, {}
        aggregate = summarize_all_questions(db)
        if not isinstance(aggregate, dict):
            return True, {}
        normalized = {}
        if isinstance(aggregate.get("questions"), list):
            for summary in aggregate["questions"]:
                item = cls._normalize_ai_audit_summary(summary)
                if item and item.get("subject_id"):
                    normalized[item["subject_id"]] = item
        else:
            for question_id, summary in aggregate.items():
                item = cls._normalize_ai_audit_summary(summary, subject_id=question_id)
                if item:
                    normalized[question_id] = item
        return True, normalized

    @classmethod
    def _ai_audit_summary(cls, db, question_id: str) -> dict | None:
        if not cls._has_ai_audit_tables(db):
            return None
        try:
            from src.ai_audit_35 import summarize_question
        except ImportError:
            return None
        summary = summarize_question(db, question_id)
        normalized = cls._normalize_ai_audit_summary(summary, subject_id=question_id)
        run_id = normalized.get("run_id") if normalized else None
        execution = cls._ai_audit_execution(db, subject_id=question_id, run_id=run_id)
        history = cls._ai_audit_history(db, question_id)
        if normalized is None and execution is None and history is None:
            return None
        if normalized is None:
            normalized = cls._normalize_ai_audit_summary({
                "subject_id": question_id,
                "total": 0,
                "clear": 0,
                "finding": 0,
                "uncertain": 0,
                "unresolved_findings": 0,
                "disagreement": False,
                "risk_score": 0,
                "risk_level": "none",
            }, subject_id=question_id)
        if execution:
            normalized["execution"] = execution
            normalized.update(cls._ai_audit_execution_list_summary(execution))
            if execution.get("latest_run"):
                normalized["latest_run"] = execution["latest_run"]
        if history:
            normalized["history"] = history
        return normalized

    @staticmethod
    def _ai_audit_history(db, question_id: str) -> dict | None:
        """Transport backend-owned multi-run history for one frozen subject."""
        try:
            from src.ai_audit_35 import question_audit_history
        except ImportError:
            return None
        history = question_audit_history(db, question_id)
        if not isinstance(history, dict):
            return None
        runs = history.get("runs")
        if not isinstance(runs, list) or not runs:
            return None
        return history

    @classmethod
    def _ai_audit_table_names(cls, db) -> set[str]:
        if isinstance(db, str) and db.strip().lower().startswith(("postgresql://", "postgres://")):
            return set(AI_AUDIT_TABLES) | {"ai_audit_checkpoints", "ai_audit_attempts"} \
                if cls._has_ai_audit_tables(db) else set()
        if getattr(db, "ai_audit_tuple_rows", False) or type(db).__name__.startswith("Postgres"):
            return set(AI_AUDIT_TABLES) | {"ai_audit_checkpoints", "ai_audit_attempts"} \
                if cls._has_ai_audit_tables(db) else set()
        return {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ai_audit_%'"
            )
        }

    @classmethod
    def _ai_audit_execution(
        cls,
        db,
        *,
        subject_id: str,
        run_id: str | None = None,
    ) -> dict | None:
        """Transport backend run/pass/attempt status without deriving audit decisions."""
        required = {"ai_audit_runs", "ai_audit_passes", "ai_audit_results", "ai_audit_checkpoints"}
        if not required.issubset(cls._ai_audit_table_names(db)):
            return None
        try:
            from src.ai_audit_35 import status_report
        except ImportError:
            return None
        # Older audit helpers exposed only run-wide execution state. Do not
        # misattribute those attempts to every question; wait for the
        # subject-scoped contract instead.
        if "subject_id" not in inspect.signature(status_report).parameters:
            return None
        report = status_report(db, run_id, subject_id=subject_id)
        return cls._normalize_ai_audit_execution(report)

    @staticmethod
    def _normalize_ai_audit_execution(report: object) -> dict | None:
        if not isinstance(report, dict):
            return None
        latest_run = dict(report["latest_run"]) if isinstance(report.get("latest_run"), dict) else None
        passes = [dict(item) for item in report.get("passes", []) if isinstance(item, dict)] \
            if isinstance(report.get("passes"), list) else []
        if latest_run is None and not passes:
            return None
        if latest_run and latest_run.get("subject_id") and not passes:
            return None
        statuses = ("succeeded", "failed", "timed_out", "invalid")
        raw_counts = latest_run.get("attempt_status_counts") if latest_run else None
        attempt_counts = {
            status: raw_counts.get(status, 0) if isinstance(raw_counts, dict) else 0
            for status in statuses
        }
        for status in statuses:
            if type(attempt_counts[status]) is not int or attempt_counts[status] < 0:
                attempt_counts[status] = 0
        attempts = [
            attempt for audit_pass in passes
            for attempt in (audit_pass.get("attempts") if isinstance(audit_pass.get("attempts"), list) else [])
            if isinstance(attempt, dict)
        ]
        if not isinstance(raw_counts, dict):
            attempt_counts = {
                status: sum(attempt.get("status") == status for attempt in attempts)
                for status in statuses
            }
        attempt_total = latest_run.get("attempt_total") if latest_run else None
        if type(attempt_total) is not int or attempt_total < 0:
            attempt_total = sum(attempt_counts.values())
        retry_count = sum(
            type(attempt.get("attempt_number")) is int and attempt["attempt_number"] > 1
            for attempt in attempts
        )
        pass_total = latest_run.get("pass_total") if latest_run else None
        if type(pass_total) is not int or pass_total < 0:
            pass_total = len(passes)
        completed_passes = latest_run.get("completed_passes") if latest_run else None
        if type(completed_passes) is not int or completed_passes < 0:
            completed_passes = sum(item.get("state") == "complete" for item in passes)
        return {
            "latest_run": latest_run,
            "passes": passes,
            "attempt_total": attempt_total,
            "attempt_status_counts": attempt_counts,
            "retry_count": retry_count,
            "incomplete_passes": max(0, pass_total - completed_passes),
            "has_partial_failures": any(attempt_counts[status] for status in ("failed", "timed_out", "invalid")),
        }

    @staticmethod
    def _normalize_ai_audit_summary(summary: object, subject_id: str | None = None) -> dict | None:
        """Normalize helper output for the browser without recomputing audit decisions."""
        if not isinstance(summary, dict) or not summary:
            return None
        resolved_subject = summary.get("subject_id")
        if not isinstance(resolved_subject, str):
            resolved_subject = subject_id if isinstance(subject_id, str) else None
        totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
        clear = summary.get("clear", totals.get("clear", 0))
        finding = summary.get("finding", totals.get("finding", 0))
        uncertain = summary.get("uncertain", totals.get("uncertain", 0))
        clear = clear if type(clear) is int and clear >= 0 else 0
        finding = finding if type(finding) is int and finding >= 0 else 0
        uncertain = uncertain if type(uncertain) is int and uncertain >= 0 else 0
        total = summary.get("total")
        total = total if type(total) is int and total >= 0 else clear + finding + uncertain
        unresolved = summary.get("unresolved_findings", 0)
        if isinstance(unresolved, list):
            unresolved_details = unresolved
            unresolved_count = len(unresolved)
        else:
            unresolved_details = summary.get("findings") if isinstance(summary.get("findings"), list) else []
            unresolved_count = unresolved if type(unresolved) is int and unresolved >= 0 else len(unresolved_details)
        entries = [dict(item) for item in summary.get("entries", []) if isinstance(item, dict)] \
            if isinstance(summary.get("entries"), list) else []
        latest_run = dict(summary["latest_run"]) if isinstance(summary.get("latest_run"), dict) else None
        latest_at = next(
            (item.get("created_at") for item in reversed(entries)
             if isinstance(item.get("created_at"), str) and item["created_at"]),
            latest_run.get("created_at") if latest_run else None,
        )
        result = dict(summary)
        if resolved_subject:
            result["subject_id"] = resolved_subject
        result.update({
            "total": total,
            "clear": clear,
            "finding": finding,
            "uncertain": uncertain,
            "unresolved_findings": unresolved_count,
            "unresolved_details": unresolved_details,
            "entries": entries,
            "latest_run": latest_run,
            "latest_at": latest_at,
        })
        return result

    @staticmethod
    def _ai_audit_list_summary(summary: dict) -> dict:
        """Keep question-list payloads small; detailed pass logs load on demand."""
        keys = (
            "run_id", "total", "clear", "finding", "uncertain",
            "unresolved_findings", "disagreement", "risk_score", "risk_level",
            "convergence", "cross_run_convergence", "latest_run", "latest_at",
        )
        return {key: summary[key] for key in keys if key in summary}

    @staticmethod
    def _ai_audit_execution_list_summary(execution: dict | None) -> dict:
        if not isinstance(execution, dict):
            return {}
        keys = (
            "attempt_total", "attempt_status_counts", "retry_count",
            "incomplete_passes", "has_partial_failures", "latest_run",
        )
        return {key: execution[key] for key in keys if key in execution}

    def _has_audio_segments(self, db) -> bool:
        if self._has_audio_segments_cache is not None:
            return self._has_audio_segments_cache
        if self.backend == "postgres":
            row = db.execute(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_name='audio_segments') AS present"
            ).fetchone()
            present = bool(row["present"])
        else:
            present = db.execute("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='audio_segments')").fetchone()[0] == 1
        self._has_audio_segments_cache = present
        return present

    def _has_ai_audit_runs(self, db) -> bool:
        if self._has_ai_runs_cache is not None:
            return self._has_ai_runs_cache
        try:
            if self.backend == "postgres":
                row = db.execute("SELECT EXISTS(SELECT 1 FROM ai_audit_runs) AS present").fetchone()
                has_runs = bool(row["present"])
            else:
                row = db.execute("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_audit_runs')").fetchone()
                if not row[0]:
                    return False
                has_runs = bool(db.execute("SELECT EXISTS(SELECT 1 FROM ai_audit_runs)").fetchone()[0])
            self._has_ai_runs_cache = has_runs
            return has_runs
        except Exception:
            return False

    def _audio_info(self, db: sqlite3.Connection, question: sqlite3.Row) -> dict | None:
        if question["section"] != "listening" or not self._has_audio_segments(db):
            return None
        sec_id = question["section_id"]
        if sec_id in self._audio_asset_cache:
            asset = self._audio_asset_cache[sec_id]
        else:
            asset = db.execute(
                "SELECT a.id,a.duration_seconds,s.sha256 FROM audio_assets a "
                "JOIN source_files s ON s.id=a.source_file_id WHERE a.section_id=?",
                (sec_id,),
            ).fetchone()
            if asset:
                self._audio_asset_cache[sec_id] = asset
        if not asset or asset["duration_seconds"] is None:
            return None
        segment = db.execute("SELECT * FROM audio_segments WHERE question_id=?",
                             (question["id"],)).fetchone()
        if segment is None:
            return None
        pair = list(SHARED_AUDIO.get(question["exam_number"], (question["exam_number"],))
                    if self.exam_id == DEFAULT_EXAM_ID else (question["exam_number"],))
        clip_url = None
        # A stored status alone never proves human listening. The evidence is
        # the reviewer's explicit declaration, not the playback UI telemetry.
        records = db.execute(
            "SELECT scope,status,evidence,reviewed_at FROM review_records "
            "WHERE subject_type='audio_segment' AND subject_id=? ORDER BY id DESC LIMIT 8",
            (question["id"],),
        ).fetchall()
        human_evidence = self._audio_evidence_from_records(
            records, segment, asset["sha256"], pair)
        if segment["status"] == "verified" and not any(
                r["scope"] == "manual_audio_boundary_35" and r["status"] == "verified"
                for r in records):
            # The display timeline is intentionally capped at eight events.
            # Repeated clip exports can push the latest human declaration out
            # of that window. Resolve the authoritative declaration from the
            # complete audit log, as the F3 bundle and export paths already do.
            # Do not expand the timeline or accept an older valid declaration
            # over a newer malformed one.
            try:
                human_evidence = self._require_audio_evidence(
                    db, question["id"], segment, asset["sha256"], pair)
            except ReviewError:
                human_evidence = None
        pair_provenance = bool(human_evidence)
        if pair_provenance:
            try:
                self._require_shared_clip_provenance(
                    db, question["id"], segment, asset["sha256"], pair)
            except ReviewError:
                pair_provenance = False
        if pair_provenance and segment["clip_relative_path"]:
            # Legacy verified status alone cannot authorize download of a clip.
            try:
                self._clip_file(segment)
                clip_url = f"/media/{question['id']}/clip"
            except ReviewError:
                pass
        return {"start_ms": segment["start_ms"], "end_ms": segment["end_ms"],
                "status": segment["status"], "version": segment["version"],
                "source_duration_ms": round(asset["duration_seconds"] * 1000),
                "shared_questions": pair, "clip_url": clip_url,
                "human_evidence": human_evidence,
                "clip_provenance_confirmed": pair_provenance,
                "review_history": [self._audio_history_item(r) for r in records]}

    @staticmethod
    def _audio_history_item(record):
        try:
            evidence = json.loads(record["evidence"])
            if not isinstance(evidence, dict):
                evidence = {}
        except (ValueError, TypeError):
            evidence = {}
        declared = evidence.get("human_evidence")
        return {"status": record["status"], "scope": record["scope"],
                "at": record["reviewed_at"],
                "human_declaration": evidence.get("verified_by_human_declaration") is True,
                "note": declared.get("note", "") if isinstance(declared, dict) and
                isinstance(declared.get("note", ""), str) else ""}

    @classmethod
    def _audio_evidence_from_records(cls, records, segment, source_sha, pair):
        """Proof of a declared review, never proof of the actual listening act.

        Fail closed on the most recent verified human-boundary record. An older
        valid event must not mask a newer malformed or mismatched declaration.
        """
        if segment["status"] != "verified":
            return None
        for record in records:
            if record["scope"] == "manual_audio_boundary_35" and record["status"] == "verified":
                return cls._audio_attestation(
                    record["evidence"], record["reviewed_at"], segment,
                    source_sha, pair)
        return None

    @classmethod
    def _require_audio_evidence(cls, db, qid, segment, source_sha, pair):
        record = db.execute(
            "SELECT scope,status,evidence,reviewed_at FROM review_records "
            "WHERE subject_type='audio_segment' AND subject_id=? "
            "AND scope='manual_audio_boundary_35' AND status='verified' "
            "ORDER BY id DESC LIMIT 1", (qid,),
        ).fetchone()
        attestation = cls._audio_evidence_from_records(
            [record] if record else [], segment, source_sha, pair)
        if attestation is None:
            raise ReviewError("Audio interval has no matching explicit human evidence; keep it unconfirmed")
        return attestation

    @classmethod
    def _require_shared_clip_provenance(cls, db, selected_id, selected, source_sha, pair):
        """Every member must carry the same source and its own audit record."""
        for number in pair:
            qid = f"035-I-L-{number:03d}"
            row = selected if qid == selected_id else db.execute(
                "SELECT * FROM audio_segments WHERE question_id=?", (qid,),
            ).fetchone()
            if not row or any(row[field] != selected[field] for field in
                    ("status", "start_ms", "end_ms", "version", "source_sha256",
                     "audio_asset_id", "clip_relative_path", "clip_sha256")):
                raise Conflict("Shared clip provenance is inconsistent; reload")
            if row["source_sha256"] != source_sha:
                raise Conflict("Shared clip source changed; reload")
            cls._require_audio_evidence(db, qid, row, source_sha, pair)

    @staticmethod
    def _audio_attestation(raw, timestamp, segment, source_sha, pair=None):
        try:
            recorded = json.loads(raw)
            attest = recorded.get("human_evidence")
            after = recorded.get("after")
            checks = ("listened_to_source", "checked_start", "checked_end", "checked_transcript")
            note = attest.get("note") if isinstance(attest, dict) else None
            if recorded.get("verified_by_human_declaration") is True and \
                    isinstance(attest, dict) and isinstance(after, dict) and \
                    all(attest.get(check) is True for check in checks) and \
                    type(attest.get("other_question_confirmed")) is bool and \
                    (pair is None or attest["other_question_confirmed"] is (len(pair) == 2)) and \
                    isinstance(note, str) and 20 <= len(note.strip()) <= 2000 and \
                    (pair is None or (len(pair) != 2 or recorded.get("shared_questions") == list(pair))) and \
                    (after.get("start_ms"), after.get("end_ms")) == \
                    (segment["start_ms"], segment["end_ms"]) and \
                    after.get("status") == "verified" and \
                    recorded.get("source_sha256") == source_sha:
                return {"note": note, "declared_at": timestamp,
                        "explicit_declaration": True}
        except (TypeError, ValueError, AttributeError, KeyError):
            pass
        return None

    def _shared_transcript_state(self, db, number: int) -> dict | None:
        """Actual 35th shared-source identity, never inferred for 36th."""
        pair = SHARED_AUDIO.get(number) if self.exam_id == DEFAULT_EXAM_ID else None
        if not pair:
            return None
        ids = sorted(f"035-I-L-{n:03d}" for n in pair)
        rows = db.execute(
            "SELECT q.id,q.review_status,t.source_file_id,t.source_pdf_page,t.dialogue_text "
            "FROM questions q LEFT JOIN transcripts t ON t.question_id=q.id "
            "WHERE q.id IN (?,?) ORDER BY q.id", ids,
        ).fetchall()
        sibling_id = next(key for key in ids if key != f"035-I-L-{number:03d}")
        coherent = len(rows) == 2 and all(r["source_file_id"] is not None for r in rows) and \
            all(rows[0][name] == rows[1][name] for name in
                ("source_file_id", "source_pdf_page", "dialogue_text"))
        sibling = next((r for r in rows if r["id"] == sibling_id), None)
        return {"other_question_id": sibling_id, "consistent": coherent,
                "other_review_status": sibling["review_status"] if sibling else None}

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _clip_output_dir(self, *, create: bool = False) -> Path:
        """Return the device-local derived clip directory without following it outside media root."""
        clip_base = self.media_root if self.backend == "postgres" else (self.root / "topik-past-papers")
        clip_base = clip_base.resolve()
        if not clip_base.exists():
            if self.backend == "postgres":
                raise ReviewError("Device-local media root is unavailable")
            # SQLite tests and explicit local roots may redirect `root` to an
            # empty sandbox. Create only this known child of the resolved root;
            # never synthesize a missing PostgreSQL media root.
            expected_parent = self.root.resolve()
            if clip_base.parent != expected_parent:
                raise ReviewError("Unsafe audio clip output directory")
            clip_base.mkdir(exist_ok=True)
        derived = clip_base / "derived"
        candidate = derived / "audio-clips"

        # Refuse an already-present symlink/junction escape before mkdir can
        # create anything through it. Recheck after each creation as defense
        # against local filesystem races.
        for existing in (derived, candidate):
            if not existing.exists():
                continue
            try:
                existing.resolve().relative_to(clip_base)
            except ValueError as exc:
                raise ReviewError("Unsafe audio clip output directory") from exc
        if create:
            derived.mkdir(exist_ok=True)
            try:
                derived.resolve().relative_to(clip_base)
            except ValueError as exc:
                raise ReviewError("Unsafe audio clip output directory") from exc
            candidate.mkdir(exist_ok=True)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(clip_base)
        except ValueError as exc:
            raise ReviewError("Unsafe audio clip output directory") from exc
        return resolved

    def _clip_path_from_relative(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative or "\\" in relative or Path(relative).is_absolute():
            raise ReviewError("Unsafe exported clip path")
        if self.backend == "postgres":
            path = self._local_media_path(relative)
        else:
            path = (self.root / relative).resolve()
        expected_root = self._clip_output_dir()
        if path.parent != expected_root or path.suffix.lower() != ".mp3":
            raise ReviewError("Unsafe exported clip path")
        return path

    def _clip_identity(self, rows: list) -> tuple[str, str, Path] | None:
        identities = {(row["clip_relative_path"], row["clip_sha256"]) for row in rows}
        if len(identities) != 1:
            raise Conflict("Shared dialogue clip metadata differs; reload and reconcile")
        relative, sha = identities.pop()
        if relative is None and sha is None:
            return None
        if not isinstance(relative, str) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            raise ReviewError("Stored clip metadata is invalid")
        return relative, sha.lower(), self._clip_path_from_relative(relative)

    def _clip_file(self, segment: sqlite3.Row) -> Path:
        relative = segment["clip_relative_path"]
        if not relative:
            raise NotFound("No exported clip")
        try:
            path = self._clip_path_from_relative(relative)
        except ReviewError as exc:
            raise NotFound("Exported clip unavailable") from exc
        if not path.is_file():
            raise NotFound("Exported clip unavailable")
        if self._file_sha256(path) != segment["clip_sha256"]:
            raise NotFound("Exported clip checksum mismatch")
        return path

    def clip_path(self, question_id: str) -> Path:
        with closing(self._connect()) as db:
            question = self._question(db, question_id)
            if not self._has_audio_segments(db):
                raise NotFound("No audio segments")
            segment = db.execute("SELECT * FROM audio_segments WHERE question_id=?",
                                 (question["id"],)).fetchone()
            if segment is None or segment["status"] != "verified":
                raise NotFound("Clip has not been verified and exported")
            pair = SHARED_AUDIO.get(question["exam_number"], (question["exam_number"],))
            source = db.execute(
                "SELECT s.sha256 FROM audio_assets a JOIN source_files s "
                "ON s.id=a.source_file_id WHERE a.id=?", (segment["audio_asset_id"],),
            ).fetchone()
            if not source or source["sha256"] != segment["source_sha256"]:
                raise Conflict("Original audio identity changed; clip not available")
            self._require_shared_clip_provenance(
                db, question["id"], segment, source["sha256"], pair)
            return self._clip_file(segment)

    def save_audio_segment(self, question_id: str, payload: dict) -> dict:
        if self.exam_id != DEFAULT_EXAM_ID:
            raise ReviewError("Audio editing for this exam is not yet supported")
        self._list_questions_cache = None
        """Persist a reviewer-specified interval; sync shared-dialogue pairs atomically.

        This NEVER modifies question/text approval or silently verifies the audio.
        """
        ordinary_fields = {"version", "start_ms", "end_ms", "status"}
        if not isinstance(payload, dict) or set(payload) not in (ordinary_fields, ordinary_fields | {"human_evidence"}):
            raise ReviewError("Expected audio version, start_ms, end_ms, status and optional human_evidence")
        version, start, end, status = (payload[k] for k in ("version", "start_ms", "end_ms", "status"))
        if type(version) is not int or version < 0 or type(start) is not int or type(end) is not int:
            raise ReviewError("Audio boundaries and version must be integer milliseconds")
        if status not in ("candidate", "verified") or not 0 <= start < end or end - start < 500:
            raise ReviewError("Audio segment must have a valid status and be at least 0.5 seconds long")
        evidence = payload.get("human_evidence")
        if status == "verified":
            required = {"listened_to_source", "checked_start", "checked_end", "checked_transcript",
                        "other_question_confirmed", "note"}
            if not isinstance(evidence, dict) or set(evidence) != required or \
                    any(evidence[name] is not True for name in required - {"note", "other_question_confirmed"}) or \
                    type(evidence["other_question_confirmed"]) is not bool or \
                    not isinstance(evidence["note"], str) or \
                    not 20 <= len(evidence["note"].strip()) <= 2000:
                raise ReviewError("Verified audio requires explicit human listening, both boundaries, transcript, paired-question checks and a detailed evidence note")
        elif evidence is not None:
            raise ReviewError("Human verification evidence is only accepted for an explicit verified decision")

        # Verify immutable local media before taking central row locks.
        with closing(self._connect()) as preflight:
            question = self._question(preflight, question_id)
            if question["section"] != "listening" or not self._has_audio_segments(preflight):
                raise ReviewError("Audio segmentation is available only after setup for listening questions")
            asset = preflight.execute(
                "SELECT a.id,a.duration_seconds,s.sha256,s.byte_size FROM audio_assets a "
                "JOIN source_files s ON s.id=a.source_file_id WHERE a.section_id=?",
                (question["section_id"],),
            ).fetchone()
            if not asset or asset["duration_seconds"] is None or end > round(asset["duration_seconds"] * 1000):
                raise ReviewError("Audio end exceeds source duration; run audio setup first")
            if end - start > 10 * 60 * 1000:
                raise ReviewError("Audio segment longer than 10 minutes needs separate handling")
            source = self.media_path(question_id, "audio")
            if source.stat().st_size != asset["byte_size"] or hashlib.sha256(source.read_bytes()).hexdigest() != asset["sha256"]:
                raise ReviewError("Audio source has changed; segment cannot be saved")
            pair = SHARED_AUDIO.get(question["exam_number"], (question["exam_number"],))
            if status == "verified" and evidence["other_question_confirmed"] is not (len(pair) == 2):
                raise ReviewError("Confirm the paired question only when the 35th source has a registered shared interval")
            qids = sorted(f"035-I-L-{number:03d}" for number in pair)
            asset_snapshot = (asset["id"], asset["duration_seconds"], asset["sha256"], asset["byte_size"])

        with self._write_transaction() as db:
            rows = self._locked_audio_rows(db, qids)
            locked_question = self._question(db, question_id)
            current_asset = db.execute(
                "SELECT a.id,a.duration_seconds,s.sha256,s.byte_size FROM audio_assets a "
                "JOIN source_files s ON s.id=a.source_file_id WHERE a.section_id=?",
                (locked_question["section_id"],),
            ).fetchone()
            if current_asset is None or (
                current_asset["id"], current_asset["duration_seconds"],
                current_asset["sha256"], current_asset["byte_size"]
            ) != asset_snapshot:
                raise Conflict("Audio source metadata changed while saving; reload before editing")
            if any((rows[qid]["version"] if qid in rows else 0) != version for qid in qids):
                raise Conflict("Audio interval was changed in another tab; reload before saving")
            if status == "verified" and any(qid not in rows or rows[qid]["status"] != "candidate" or
                    rows[qid]["start_ms"] != start or rows[qid]["end_ms"] != end for qid in qids):
                raise Conflict("Verify only the exact currently saved candidate for every shared question; reload first")
            if status == "verified" and len(qids) == 2:
                shared = self._shared_transcript_state(db, locked_question["exam_number"])
                if not shared or not shared["consistent"]:
                    raise Conflict("Shared transcripts or source pages differ. Compare both original transcripts before approving audio")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for qid in qids:
                old = rows.get(qid)
                prior = ({"start_ms": old["start_ms"], "end_ms": old["end_ms"],
                          "status": old["status"]} if old else None)
                after = {"start_ms": start, "end_ms": end, "status": status}
                if prior == after:
                    continue
                db.execute(
                    "INSERT INTO audio_segments(question_id,audio_asset_id,start_ms,end_ms,status,version,source_sha256,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(question_id) DO UPDATE SET "
                    "start_ms=excluded.start_ms,end_ms=excluded.end_ms,status=excluded.status,"
                    "version=audio_segments.version+1,updated_at=excluded.updated_at,"
                    "clip_relative_path=NULL,clip_sha256=NULL",
                    (qid, current_asset["id"], start, end, status, 1, current_asset["sha256"], now),
                )
                db.execute("INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                           "VALUES(?,?,?,?,?,?,?)",
                           ("audio_segment", qid, status, "local_reviewer", "manual_audio_boundary_35",
                            json.dumps({"before": prior, "after": after, "source_sha256": current_asset["sha256"],
                                        **({"human_evidence": evidence, "verified_by_human_declaration": True,
                                            "shared_questions": list(pair)} if status == "verified" else {})},
                                       ensure_ascii=False), now))
        return self.get_question(question_id)

    def export_audio_clip(self, question_id: str) -> dict:
        if self.exam_id != DEFAULT_EXAM_ID:
            raise ReviewError("Audio clip export for this exam is not yet supported")
        self._list_questions_cache = None
        """Export/rematerialize a verified interval while keeping the source MP3 immutable."""
        from src.audio_35 import AudioError, export_segment

        # Preflight immutable local media and the complete shared-pair state before
        # invoking the encoder or taking PostgreSQL row locks.
        with closing(self._connect()) as db:
            question = self._question(db, question_id)
            if question["section"] != "listening" or not self._has_audio_segments(db):
                raise ReviewError("Only reviewed listening intervals can be exported")
            pair = SHARED_AUDIO.get(question["exam_number"], (question["exam_number"],))
            qids = sorted(f"035-I-L-{number:03d}" for number in pair)
            rows = [db.execute("SELECT * FROM audio_segments WHERE question_id=?", (qid,)).fetchone()
                    for qid in qids]
            if any(row is None or row["status"] != "verified" for row in rows):
                raise ReviewError("Verify every linked interval before exporting an MP3")
            expected = (
                rows[0]["start_ms"], rows[0]["end_ms"], rows[0]["version"],
                rows[0]["source_sha256"], rows[0]["audio_asset_id"],
            )
            if any((row["start_ms"], row["end_ms"], row["version"], row["source_sha256"],
                    row["audio_asset_id"]) != expected for row in rows):
                raise Conflict("Shared dialogue bounds or source differ; reload and reconcile")
            canonical_before = self._clip_identity(rows)
            start, end, version, expected_sha, audio_asset_id = expected
            audio_source = db.execute(
                "SELECT s.id AS source_file_id,s.relative_path,s.sha256,s.byte_size FROM audio_assets a "
                "JOIN source_files s ON s.id=a.source_file_id WHERE a.id=?", (audio_asset_id,)
            ).fetchone()
            if audio_source is None or audio_source["sha256"] != expected_sha:
                raise ReviewError("The segment's original audio checksum no longer matches")
            for qid, segment in zip(qids, rows):
                self._require_audio_evidence(db, qid, segment, expected_sha, pair)
            source_snapshot = (
                audio_source["source_file_id"], audio_source["relative_path"],
                audio_source["sha256"], audio_source["byte_size"],
            )

        source = self.media_path(question_id, "audio")
        if (not source.is_file() or source.stat().st_size != audio_source["byte_size"] or
                self._file_sha256(source) != expected_sha):
            raise ReviewError("The original recording changed; export stopped")
        output_dir = self._clip_output_dir(create=True)

        # A canonical DB identity plus an intact local file is already complete.
        # Do not encode and do not append duplicate export history.
        if canonical_before is not None:
            _, canonical_sha, canonical_path = canonical_before
            if canonical_path.exists():
                if not canonical_path.is_file() or self._file_sha256(canonical_path) != canonical_sha:
                    raise Conflict("Local canonical clip differs from the central checksum")
                return self.get_question(question_id)

        descriptor, temp_name = tempfile.mkstemp(prefix=".audio-export-", suffix=".mp3", dir=output_dir)
        os.close(descriptor)
        temporary = Path(temp_name)
        # export_segment refuses any existing destination; remove only our own
        # reserved zero-byte placeholder and let it create this unique temp path.
        temporary.unlink()
        try:
            try:
                export_segment(
                    source, temporary, start, end,
                    expected_source_sha256=expected_sha,
                )
            except AudioError as exc:
                raise ReviewError(f"Audio clip export failed: {exc}") from exc
            if (not temporary.is_file() or temporary.parent.resolve() != output_dir or
                    temporary.stat().st_size < 1000):
                raise ReviewError("FFmpeg produced an empty or invalid clip")
            generated_sha = self._file_sha256(temporary)
            if (not source.is_file() or source.stat().st_size != source_snapshot[3] or
                    self._file_sha256(source) != expected_sha):
                raise Conflict("The original recording changed while exporting; no clip was linked")

            owned_publication: dict[str, object | None] = {"path": None, "sha": None}

            def cleanup_owned_publication():
                path = owned_publication["path"]
                sha = owned_publication["sha"]
                if not isinstance(path, Path) or not isinstance(sha, str) or not path.is_file():
                    return
                if self._file_sha256(path) == sha:
                    path.unlink(missing_ok=True)

            with self._write_transaction(before_rollback=cleanup_owned_publication) as db:
                locked = self._locked_audio_rows(db, qids)
                if any(qid not in locked for qid in qids):
                    raise Conflict("Audio interval changed while exporting; no clip was linked")
                current = [locked[qid] for qid in qids]
                if any(row["status"] != "verified" or
                       (row["start_ms"], row["end_ms"], row["version"], row["source_sha256"],
                        row["audio_asset_id"]) != expected for row in current):
                    raise Conflict("Audio interval changed while exporting; no clip was linked")
                current_source = db.execute(
                    "SELECT s.id AS source_file_id,s.relative_path,s.sha256,s.byte_size FROM audio_assets a "
                    "JOIN source_files s ON s.id=a.source_file_id WHERE a.id=?", (audio_asset_id,)
                ).fetchone()
                if current_source is None or (
                    current_source["source_file_id"], current_source["relative_path"],
                    current_source["sha256"], current_source["byte_size"],
                ) != source_snapshot:
                    raise Conflict("Audio source metadata changed while exporting; no clip was linked")
                for qid, segment in zip(qids, current):
                    self._require_audio_evidence(db, qid, segment, expected_sha, pair)

                canonical_now = self._clip_identity(current)
                if canonical_now is None:
                    name = f"035-I-L-{'-'.join(f'{n:03d}' for n in pair)}-v{version}-{generated_sha[:12]}.mp3"
                    relative = f"topik-past-papers/derived/audio-clips/{name}"
                    destination = self._clip_path_from_relative(relative)
                    canonical_sha = generated_sha
                else:
                    relative, canonical_sha, destination = canonical_now
                    if generated_sha != canonical_sha:
                        raise Conflict("Generated clip checksum differs from the central canonical clip")

                # Atomic no-overwrite publication. If another local process won
                # the filesystem race, reuse only byte-identical content.
                if destination.exists():
                    if not destination.is_file() or self._file_sha256(destination) != canonical_sha:
                        raise Conflict("Export destination already contains different audio")
                else:
                    try:
                        os.link(temporary, destination)
                        owned_publication["path"] = destination
                        owned_publication["sha"] = canonical_sha
                    except FileExistsError:
                        if (not destination.is_file() or
                                self._file_sha256(destination) != canonical_sha):
                            raise Conflict("Export destination appeared with different audio")

                # A pre-existing canonical identity means this device merely
                # materialized the local artifact. Do not duplicate DB metadata
                # or review history.
                if canonical_now is None:
                    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    for qid in qids:
                        db.execute(
                            "UPDATE audio_segments SET clip_relative_path=?,clip_sha256=? WHERE question_id=?",
                            (relative, canonical_sha, qid),
                        )
                        db.execute(
                            "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            ("audio_segment", qid, "verified", "local_reviewer", "audio_export_35",
                             json.dumps({"clip": relative, "sha256": canonical_sha,
                                         "source_sha256": expected_sha, "start_ms": start,
                                         "end_ms": end}, ensure_ascii=False), now),
                        )
        finally:
            temporary.unlink(missing_ok=True)
        return self.get_question(question_id)

    def _get_ai_audit_for_question(self, db, question_id: str) -> tuple[bool, dict | None]:
        if not self._has_ai_audit_tables(db):
            return False, None
        if self.backend == "postgres":
            if not self.database_url:
                return False, None
            try:
                from src.database import PostgresAuditConnection
                with closing(PostgresAuditConnection(self.database_url, readonly=True)) as audit_conn:
                    return True, self._ai_audit_summary(audit_conn, question_id)
            except Exception:
                return True, self._ai_audit_summary(self.database_url, question_id)
        return True, self._ai_audit_summary(db, question_id)

    def get_independent_audit_comparison(self) -> dict:
        """Present complete archived AI judgments side by side, without mutating audit history."""
        if self.exam_id != DEFAULT_EXAM_ID:
            return {"exam_id": self.exam_id, "sources": {}, "questions": {},
                    "disagreement_count": 0, "available": False}
        results: dict[str, dict] = {"gemini": {}, "chatgpt": {}}
        sources: dict[str, dict] = {
            "gemini": {"available": False, "label": "Gemini", "reason": "70문항 Gemini 결과를 찾지 못했습니다."},
            "chatgpt": {"available": False, "label": "ChatGPT 3차", "reason": "3차 독립 감수 JSON이 없습니다."},
        }

        if THIRD_PASS_AUDIT_PATH.is_file():
            archive_bytes = THIRD_PASS_AUDIT_PATH.read_bytes()
            payload = json.loads(archive_bytes.decode("utf-8"))
            entries = payload.get("results", [])
            if (payload.get("contract_version") == "ai-audit-35-v1"
                    and len(entries) == 70
                    and sorted(row.get("exam_number") for row in entries) == list(range(1, 71))
                    and all(row.get("verdict") in ("clear", "finding", "uncertain") for row in entries)):
                # Archive metadata must come from the archive itself. Its file
                # name or modification date is not a trustworthy audit time.
                time_field = next((field for field in ("created_at", "generated_at", "audited_at")
                                   if payload.get(field)), None)
                try:
                    source_name = str(THIRD_PASS_AUDIT_PATH.relative_to(ROOT)).replace("\\", "/")
                except ValueError:
                    source_name = THIRD_PASS_AUDIT_PATH.name
                results["chatgpt"] = {
                    str(row["exam_number"]): {
                        "verdict": row["verdict"],
                        "summary": row.get("summary"),
                        "detail": row.get("detail"),
                        "created_at": (payload[time_field] if time_field else "unknown"),
                        "timestamp_verified": bool(time_field and self._audit_timestamp_verified(payload[time_field])),
                        "snapshot_created_at": "unknown",
                        "snapshot_timestamp_verified": False,
                    } for row in entries
                }
                sources["chatgpt"] = {"available": True, "label": "ChatGPT 3차", "model": payload.get("model", ""),
                                       "auditor": payload.get("auditor"), "scope": 70,
                                       "origin": "로컬 3차 독립 감수 JSON",
                                       "source_file": source_name,
                                       "source_sha256": hashlib.sha256(archive_bytes).hexdigest(),
                                       "contract_version": payload["contract_version"],
                                       "created_at": payload[time_field] if time_field else "unknown",
                                       "timestamp_source": time_field or "unknown",
                                       "timestamp_verified": bool(time_field and self._audit_timestamp_verified(payload[time_field]))}
            else:
                sources["chatgpt"]["reason"] = "3차 독립 감수 파일의 계약 또는 70문항 범위가 유효하지 않습니다."

        with closing(self._connect()) as db:
            if self._has_ai_audit_tables(db):
                rows = db.execute(
                    "SELECT p.id AS pass_id, p.pass_number, p.model_id, p.auditor_id,"
                    " p.prompt_version, p.perspective, p.input_sha256,"
                    " p.created_at AS pass_created_at, r.id AS result_id,"
                    " r.result_sha256, r.created_at AS result_created_at, r.raw_json,"
                    " run.id AS run_id, run.label AS run_label,"
                    " run.contract_version, run.snapshot_sha256 AS source_snapshot_sha256,"
                    " run.created_at AS run_created_at,"
                    " snapshot.created_at AS source_snapshot_created_at "
                    "FROM ai_audit_results r "
                    "JOIN ai_audit_passes p ON p.id=r.pass_id "
                    "JOIN ai_audit_runs run ON run.id=p.run_id "
                    "JOIN ai_audit_source_snapshots snapshot ON snapshot.snapshot_sha256=run.snapshot_sha256 "
                    "WHERE run.label=? AND p.model_id LIKE ? "
                    "ORDER BY run.created_at DESC, p.pass_number DESC, r.created_at DESC",
                    ("gemini-full-audit-70", "gemini%"),
                ).fetchall()
                for row in rows:
                    payload = json.loads(row["raw_json"])
                    verdicts = payload.get("verdicts", [])
                    indexed = {}
                    for entry in verdicts:
                        subject_id = entry.get("subject_id", "")
                        if not isinstance(subject_id, str) or not QUESTION_ID.fullmatch(subject_id):
                            continue
                        n = str(int(subject_id[-3:]))
                        if n in indexed:
                            break
                        indexed[n] = {"verdict": entry.get("verdict"),
                                      "summary": entry.get("rationale", ""),
                                      "confidence": entry.get("confidence"),
                                      "created_at": row["result_created_at"] or "unknown",
                                      "timestamp_verified": self._audit_timestamp_verified(row["result_created_at"]),
                                      "snapshot_created_at": row["source_snapshot_created_at"] or "unknown",
                                      "snapshot_timestamp_verified": self._audit_timestamp_verified(
                                          row["source_snapshot_created_at"])}
                    if len(indexed) == 70 and set(indexed) == {str(n) for n in range(1, 71)} and all(
                        entry["verdict"] in ("clear", "finding", "uncertain") for entry in indexed.values()
                    ):
                        results["gemini"] = indexed
                        sources["gemini"] = {"available": True, "label": "Gemini",
                                             "model": row["model_id"], "auditor": row["auditor_id"],
                                             "scope": 70,
                                             "origin": ("중앙 PostgreSQL" if self.backend == "postgres"
                                                        else "로컬 SQLite") + " gemini-full-audit-70 실행 결과",
                                             "created_at": row["result_created_at"],
                                             "timestamp_verified": self._audit_timestamp_verified(row["result_created_at"]),
                                             "timestamp_source": "ai_audit_results.created_at",
                                             "run_created_at": row["run_created_at"],
                                             "pass_created_at": row["pass_created_at"],
                                             "result_created_at": row["result_created_at"],
                                             "source_snapshot_created_at": row["source_snapshot_created_at"],
                                             "run_id": row["run_id"], "run_label": row["run_label"],
                                             "pass_id": row["pass_id"], "pass_number": row["pass_number"],
                                             "result_id": row["result_id"],
                                             "source_snapshot_sha256": row["source_snapshot_sha256"],
                                             "input_sha256": row["input_sha256"],
                                             "result_sha256": row["result_sha256"],
                                             "contract_version": row["contract_version"],
                                             "prompt_version": row["prompt_version"],
                                             "perspective": row["perspective"]}
                        break

        questions = {}
        for number in range(1, 71):
            key = str(number)
            gemini = results["gemini"].get(key)
            chatgpt = results["chatgpt"].get(key)
            questions[key] = {"gemini": gemini, "chatgpt": chatgpt,
                              "disagreement": bool(gemini and chatgpt and gemini["verdict"] != chatgpt["verdict"])}
        return {"sources": sources, "questions": questions,
                "disagreement_count": sum(q["disagreement"] for q in questions.values())}

    def get_question(self, question_id: str, *, fast: bool = False) -> dict:
        with closing(self._connect()) as db:
            # Concurrent reviews or source migrations must never combine old
            # editor fields with a newer optimistic review version. Every exam
            # needs one stable snapshot for the complete editor payload.
            if self.backend == "postgres":
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            elif self.backend == "sqlite":
                db.execute("BEGIN")
            row = self._question(db, question_id)
            question = dict(row)
            ai_audit = None if fast or self.exam_id != DEFAULT_EXAM_ID else self._get_ai_audit_for_question(db, question_id)[1]
            choices = [dict(item) for item in db.execute(
                "SELECT number, text FROM choices WHERE question_id=? ORDER BY number", (question_id,)
            )]
            transcript = db.execute(
                "SELECT dialogue_text AS text, source_pdf_page, review_status, warnings_json "
                "FROM transcripts WHERE question_id=?", (question_id,)
            ).fetchone()
            transcript_info = dict(transcript) if transcript else None
            if transcript_info:
                transcript_info["warnings"] = json.loads(transcript_info.pop("warnings_json"))
            image_keys = [item["image_key"] for item in db.execute(
                "SELECT image_key FROM question_images WHERE question_id=? ORDER BY image_key", (question_id,)
            )]
            history = []
            records = db.execute(
                "SELECT id, status, scope, reviewer, evidence, reviewed_at FROM review_records "
                "WHERE subject_type='question' AND subject_id=? ORDER BY id DESC LIMIT 30", (question_id,)
            ).fetchall()
            for record in records:
                history.append(self._question_history_item(record))
            manual = next((r for r in records if r["scope"] == "manual_question_review"), None)
            if manual is None and len(records) == 30:
                # Preserve provenance even when the latest manual action is
                # older than the bounded visible history.
                manual = db.execute(
                    "SELECT id,status,reviewer,reviewed_at FROM review_records "
                    "WHERE subject_type='question' AND subject_id=? "
                    "AND scope='manual_question_review' ORDER BY id DESC LIMIT 1",
                    (question_id,),
                ).fetchone()
            last_human, human_evidence = self._question_human_evidence(
                question["review_status"], records[0] if records else None, manual)
            version = (len(records) + self._punctuation_revision(db)
                       if len(records) < 30 else self._version(db, question_id))
            result = {
                "id": question_id,
                "exam_id": self.exam_id,
                "number": question["exam_number"],
                "section": question["section"],
                "review_status": question["review_status"],
                "version": version,
                "stem": question["stem"],
                "raw_question_text": question["raw_question_text"],
                "raw_question_text_display": (
                    normalize_punctuation_spacing_v3(question["raw_question_text"])
                    if self.exam_id == "036-I-B" else question["raw_question_text"]
                ),
                "group": {"instruction": question["instruction"] or "",
                          "passage_text": question["passage_text"] or "",
                          "start": question["first_exam_number"],
                          "end": question["last_exam_number"]},
                "choices": choices,
                "answer": {"choice_number": question["choice_number"],
                           "source_pdf_page": question["answer_pdf_page"]},
                "points": question["points"],
                "source_pdf_page": question["source_pdf_page"],
                "answer_key_number": question["answer_key_number"],
                "source_pdf_url": self._media_url(question_id, "paper", page=question["source_pdf_page"]),
                "answer_pdf_url": self._media_url(question_id, "answer", page=question["answer_pdf_page"]),
                "transcript": transcript_info,
                "transcript_pdf_url": (self._media_url(question_id, "transcript", page=transcript_info["source_pdf_page"])
                                       if transcript_info else None),
                "audio_url": self._media_url(question_id, "audio") if transcript_info else None,
                "audio_segment": self._audio_info(db, row),
                "images": [{"url": self._media_url(question_id, f"image/{index}"), "key": key}
                           for index, key in enumerate(image_keys)],
                "requires_image": bool(question["requires_image"]),
                "preview_flags": json.loads(question["preview_flags_json"]),
                "history": history,
                "last_human_review": last_human,
                "human_review_evidence": human_evidence,
            }
            shared_state = self._shared_transcript_state(db, question["exam_number"])
            if shared_state:
                result["shared_transcript_state"] = shared_state
            if ai_audit:
                result["ai_audit"] = ai_audit
            return self._add_punctuation_preview(result)

    def get_questions_bundle(self) -> dict:
        with closing(self._connect()) as db:
            # Keep every component of the bundle on one database snapshot.
            # READ COMMITTED would allow old question text to be combined with
            # a newer review version, defeating optimistic concurrency.
            if self.backend == "postgres":
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            else:
                db.execute("BEGIN")
            rows = db.execute(
                "SELECT q.*, s.name AS section, g.instruction, g.passage_text, "
                "g.first_exam_number, g.last_exam_number, a.choice_number, "
                "a.source_file_id AS answer_file_id, a.source_pdf_page AS answer_pdf_page "
                "FROM questions q JOIN sections s ON s.id=q.section_id "
                "LEFT JOIN question_groups g ON g.id=q.group_id "
                "JOIN answers a ON a.question_id=q.id "
                "WHERE s.exam_id=? ORDER BY q.exam_number", (self.exam_id,)
            ).fetchall()

            choice_rows = db.execute(
                "SELECT c.question_id,c.number,c.text FROM choices c "
                "JOIN questions q ON q.id=c.question_id "
                "JOIN sections s ON s.id=q.section_id "
                "WHERE s.exam_id=? ORDER BY c.question_id,c.number", (self.exam_id,)
            ).fetchall()
            choices_by_qid: dict[str, list[dict]] = defaultdict(list)
            for c in choice_rows:
                choices_by_qid[c["question_id"]].append({"number": c["number"], "text": c["text"]})

            transcript_rows = db.execute(
                "SELECT t.question_id,t.dialogue_text AS text,t.source_file_id,t.source_pdf_page,"
                "t.review_status,t.warnings_json FROM transcripts t "
                "JOIN questions q ON q.id=t.question_id "
                "JOIN sections s ON s.id=q.section_id WHERE s.exam_id=?", (self.exam_id,)
            ).fetchall()
            transcript_by_qid = {}
            for t in transcript_rows:
                item = dict(t)
                qid = item.pop("question_id")
                item.pop("source_file_id", None)
                try:
                    item["warnings"] = json.loads(item.pop("warnings_json"))
                except (ValueError, TypeError):
                    item["warnings"] = []
                transcript_by_qid[qid] = item

            # Shared-source consistency derives from the same snapshot as
            # the bundle. No extra roundtrip and no 36th pair inference.
            if self.exam_id == DEFAULT_EXAM_ID:
                source_by_qid = {r["question_id"]: r for r in transcript_rows}
                shared_states = {}
                row_by_id = {row["id"]: row for row in rows}
                for number, pair in SHARED_AUDIO.items():
                    current, other = (f"035-I-L-{n:03d}" for n in pair)
                    a, b = source_by_qid.get(current), source_by_qid.get(other)
                    # Source identity itself is checked in get_question and
                    # before any write; bundle reports text/page consistency.
                    consistent = bool(a and b and a["source_file_id"] == b["source_file_id"] and
                                      a["text"] == b["text"] and
                                      a["source_pdf_page"] == b["source_pdf_page"])
                    selected = f"035-I-L-{number:03d}"
                    sibling = other if selected == current else current
                    shared_states[selected] = {"other_question_id": sibling,
                        "consistent": consistent,
                        "other_review_status": row_by_id[sibling]["review_status"] if sibling in row_by_id else None}
            else:
                shared_states = {}

            image_rows = db.execute(
                "SELECT qi.question_id,qi.image_key FROM question_images qi "
                "JOIN questions q ON q.id=qi.question_id "
                "JOIN sections s ON s.id=q.section_id "
                "WHERE s.exam_id=? ORDER BY qi.question_id,qi.image_key", (self.exam_id,)
            ).fetchall()
            images_by_qid: dict[str, list[str]] = defaultdict(list)
            for img in image_rows:
                images_by_qid[img["question_id"]].append(img["image_key"])

            record_rows = db.execute(
                "SELECT id,subject_type,subject_id,status,scope,reviewer,evidence,reviewed_at FROM review_records "
                "WHERE subject_type IN ('question','audio_segment') AND subject_id IN ("
                "SELECT q.id FROM questions q JOIN sections s ON s.id=q.section_id "
                "WHERE s.exam_id=?) ORDER BY id DESC", (self.exam_id,)
            ).fetchall()
            records_by_qid: dict[str, list[dict]] = defaultdict(list)
            audio_history_by_qid: dict[str, list[dict]] = defaultdict(list)
            count_by_qid: dict[str, int] = defaultdict(int)
            latest_by_qid = {}
            manual_by_qid = {}
            for r in record_rows:
                qid = r["subject_id"]
                if r["subject_type"] == "audio_segment":
                    if len(audio_history_by_qid[qid]) < 8:
                        audio_history_by_qid[qid].append(self._audio_history_item(r))
                    continue
                count_by_qid[qid] += 1
                if qid not in latest_by_qid:
                    latest_by_qid[qid] = r
                if r["scope"] == "manual_question_review" and qid not in manual_by_qid:
                    manual_by_qid[qid] = r
                if len(records_by_qid[qid]) < 30:
                    records_by_qid[qid].append(self._question_history_item(r))

            has_audio = self._has_audio_segments(db)
            assets_by_sec = {}
            segments_by_qid = {}
            audio_attestations = {}
            if has_audio:
                asset_rows = db.execute(
                    "SELECT a.id, a.section_id, a.duration_seconds, s.sha256 FROM audio_assets a "
                    "JOIN source_files s ON s.id=a.source_file_id "
                    "JOIN sections sec ON sec.id=a.section_id "
                    "WHERE sec.exam_id=?", (self.exam_id,)
                ).fetchall()
                assets_by_sec = {r["section_id"]: r for r in asset_rows}
                for sec_id, asset in assets_by_sec.items():
                    self._audio_asset_cache[sec_id] = asset

                segment_rows = db.execute(
                    "SELECT a.* FROM audio_segments a "
                    "JOIN questions q ON q.id=a.question_id "
                    "JOIN sections s ON s.id=q.section_id WHERE s.exam_id=?",
                    (self.exam_id,),
                ).fetchall()
                segments_by_qid = {r["question_id"]: r for r in segment_rows}
                if any(r["status"] == "verified" for r in segment_rows):
                    # One batched query, not a per-question F3 read. Legacy
                    # status-only records are marked as evidence unconfirmed.
                    evidence_rows = db.execute(
                        "SELECT r.subject_id,r.evidence,r.reviewed_at FROM review_records r "
                        "JOIN questions q ON q.id=r.subject_id "
                        "JOIN sections s ON s.id=q.section_id "
                        "WHERE s.exam_id=? AND r.subject_type='audio_segment' "
                        "AND r.scope='manual_audio_boundary_35' AND r.status='verified' "
                        "ORDER BY r.id DESC", (self.exam_id,),
                    ).fetchall()
                    for evidence_row in evidence_rows:
                        qid = evidence_row["subject_id"]
                        if qid not in audio_attestations:
                            audio_attestations[qid] = evidence_row

            punctuation_revision = self._punctuation_revision(db)
            questions_map = {}
            for row in rows:
                question = dict(row)
                qid = question["id"]

                transcript_info = transcript_by_qid.get(qid)
                image_keys = images_by_qid.get(qid, [])
                history = records_by_qid.get(qid, [])
                version = count_by_qid.get(qid, 0) + punctuation_revision
                last_human, human_evidence = self._question_human_evidence(
                    question["review_status"], latest_by_qid.get(qid), manual_by_qid.get(qid))

                audio_segment = None
                if question["section"] == "listening" and has_audio:
                    asset = assets_by_sec.get(question["section_id"])
                    if asset and asset["duration_seconds"] is not None:
                        segment = segments_by_qid.get(qid)
                        if segment is not None:
                            pair = list(SHARED_AUDIO.get(question["exam_number"], (question["exam_number"],))
                                        if self.exam_id == DEFAULT_EXAM_ID else (question["exam_number"],))
                            clip_url = None
                            evidence = (self._audio_attestation(
                                audio_attestations[qid]["evidence"],
                                audio_attestations[qid]["reviewed_at"], segment,
                                asset["sha256"], pair) if qid in audio_attestations else None)
                            # The clip is a shared source; do not publish an
                            # apparently usable URL if its partner's review
                            # attestation is missing or its version differs.
                            pair_provenance = bool(evidence)
                            for number in pair:
                                linked_id = f"035-I-L-{number:03d}"
                                linked = segments_by_qid.get(linked_id)
                                linked_audit = audio_attestations.get(linked_id)
                                if (not linked or not linked_audit or
                                    any(linked[key] != segment[key] for key in
                                        ("status", "start_ms", "end_ms", "version",
                                         "source_sha256", "audio_asset_id", "clip_relative_path", "clip_sha256")) or
                                    not self._audio_attestation(linked_audit["evidence"],
                                        linked_audit["reviewed_at"], linked, asset["sha256"], pair)):
                                    pair_provenance = False
                                    break
                            if pair_provenance and segment["clip_relative_path"]:
                                try:
                                    self._clip_file(segment)
                                    clip_url = self._media_url(qid, "clip")
                                except ReviewError:
                                    pass
                            audio_segment = {
                                "start_ms": segment["start_ms"],
                                "end_ms": segment["end_ms"],
                                "status": segment["status"],
                                "version": segment["version"],
                                "source_duration_ms": round(asset["duration_seconds"] * 1000),
                                "shared_questions": pair,
                                "clip_url": clip_url,
                                "human_evidence": evidence,
                                "clip_provenance_confirmed": pair_provenance,
                                "review_history": audio_history_by_qid.get(qid, []),
                            }

                q_data = {
                    "id": qid,
                    "exam_id": self.exam_id,
                    "number": question["exam_number"],
                    "section": question["section"],
                    "review_status": question["review_status"],
                    "version": version,
                    "stem": question["stem"],
                    "raw_question_text": question["raw_question_text"],
                    "raw_question_text_display": (
                        normalize_punctuation_spacing_v3(question["raw_question_text"])
                        if self.exam_id == "036-I-B" else question["raw_question_text"]
                    ),
                    "group": {
                        "instruction": question["instruction"] or "",
                        "passage_text": question["passage_text"] or "",
                        "start": question["first_exam_number"],
                        "end": question["last_exam_number"],
                    },
                    "choices": choices_by_qid.get(qid, []),
                    "answer": {
                        "choice_number": question["choice_number"],
                        "source_pdf_page": question["answer_pdf_page"],
                    },
                    "points": question["points"],
                    "source_pdf_page": question["source_pdf_page"],
                    "answer_key_number": question["answer_key_number"],
                    "source_pdf_url": self._media_url(qid, "paper", page=question["source_pdf_page"]),
                    "answer_pdf_url": self._media_url(qid, "answer", page=question["answer_pdf_page"]),
                    "transcript": transcript_info,
                    "transcript_pdf_url": (
                        self._media_url(qid, "transcript", page=transcript_info["source_pdf_page"])
                        if transcript_info else None
                    ),
                    "audio_url": self._media_url(qid, "audio") if transcript_info else None,
                    "audio_segment": audio_segment,
                    "images": [
                        {"url": self._media_url(qid, f"image/{index}"), "key": key}
                        for index, key in enumerate(image_keys)
                    ],
                    "requires_image": bool(question["requires_image"]),
                    "preview_flags": json.loads(question["preview_flags_json"]),
                    "history": history,
                    "last_human_review": last_human,
                    "human_review_evidence": human_evidence,
                }
                if qid in shared_states:
                    q_data["shared_transcript_state"] = shared_states[qid]
                questions_map[qid] = self._add_punctuation_preview(q_data)

            return {
                "exam_id": self.exam_id,
                "total_questions": len(questions_map),
                "questions": questions_map,
            }

    @staticmethod
    def _validate_payload(payload: dict, question: sqlite3.Row, existing: dict) -> dict:
        if not isinstance(payload, dict):
            raise ReviewError("Expected JSON object")
        allowed = {"version", "status", "stem", "choices", "transcript_text", "note"}
        if set(payload) not in (allowed, allowed | {"shared_transcript"}):
            raise ReviewError("Review payload has unexpected fields")
        if type(payload["version"]) is not int or payload["version"] < 0:
            raise ReviewError("Invalid review version")
        if payload["status"] not in STATUSES:
            raise ReviewError("Invalid review status")
        if not isinstance(payload["stem"], str) or len(payload["stem"]) > 10000:
            raise ReviewError("Question text exceeds limit")
        choices = payload["choices"]
        if not isinstance(choices, list) or len(choices) != 4 or any(
                not isinstance(text, str) or len(text) > 10000 for text in choices):
            raise ReviewError("Exactly four text choices are required")
        if not any(text.strip() for text in choices) and not question["requires_image"]:
            raise ReviewError("Non-image question cannot have four blank choices")
        transcript = payload["transcript_text"]
        if existing["transcript"]:
            if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > 20000:
                raise ReviewError("Listening transcript must contain text")
        elif transcript is not None:
            raise ReviewError("Reading question has no transcript to edit")
        if not isinstance(payload["note"], str) or len(payload["note"]) > 4000:
            raise ReviewError("Invalid review note")
        if payload["status"] == "rejected" and not payload["note"].strip():
            raise ReviewError("A rejection reason is required")
        if "shared_transcript" in payload:
            shared = payload["shared_transcript"]
            if not isinstance(shared, dict) or set(shared) not in (
                {"other_question_id", "other_version", "confirm_shared_source"},
                {"other_question_id", "other_version", "confirm_shared_source", "confirm_reset_review"},
            ) or type(shared["other_version"]) is not int or shared["other_version"] < 0 \
                    or shared["confirm_shared_source"] is not True or \
                    not isinstance(shared["other_question_id"], str) or \
                    ("confirm_reset_review" in shared and shared["confirm_reset_review"] is not True):
                raise ReviewError("Shared transcript requires explicit paired question/version confirmation")
            if not payload["note"].strip():
                raise ReviewError("Shared transcript correction requires an original-source review note")
        return payload

    def _locked_shared_transcripts(self, db, question_id: str, number: int, new_text: str,
                                   shared: dict | None) -> dict | None:
        """Guard a real 35th common dialogue; never silently edit a sibling.

        This runs inside the existing review write transaction. The caller
        locks both question IDs in sorted order *before* this check; audio
        boundary writers use the same order. Versions and source provenance
        are checked while the locks are held. The sibling's human status is
        never promoted, rejected or changed implicitly.
        """
        pair = SHARED_AUDIO.get(number) if self.exam_id == DEFAULT_EXAM_ID else None
        if not pair:
            if shared is not None:
                raise ReviewError("This exam/question has no confirmed shared-transcript correction")
            return None
        qids = sorted(f"035-I-L-{n:03d}" for n in pair)
        if question_id not in qids:
            raise Conflict("Shared transcript source/question mapping is inconsistent")
        sibling_id = next(qid for qid in qids if qid != question_id)
        if shared is None:
            raise Conflict(f"{pair[0]}·{pair[1]}번 공유 대본은 한쪽만 수정할 수 없습니다. 짝 문항을 대조하고 명시적으로 함께 수정하세요.")
        if shared["other_question_id"] != sibling_id:
            raise Conflict("Shared transcript sibling is not the registered 35th source pair")
        sibling = self._question(db, sibling_id)
        if sibling["section"] != "listening" or sibling["exam_number"] not in pair:
            raise Conflict("The paired source is not a registered 35th listening question")
        source_rows = db.execute(
            "SELECT question_id,source_file_id,source_pdf_page,dialogue_text,review_status "
            "FROM transcripts WHERE question_id IN (?,?) ORDER BY question_id",
            qids,
        ).fetchall()
        if len(source_rows) != 2:
            raise Conflict("Shared dialogue provenance is incomplete; no transcript was changed")
        by_id = {r["question_id"]: r for r in source_rows}
        first, second = (by_id[qid] for qid in qids)
        if first["source_file_id"] != second["source_file_id"] or \
                first["source_pdf_page"] != second["source_pdf_page"] or \
                first["dialogue_text"] != second["dialogue_text"]:
            raise Conflict("Shared dialogue records already differ; reconcile source and individual history before editing")
        original_status = sibling["review_status"]
        original_transcript_status = by_id[sibling_id]["review_status"]
        recheck_required = original_status != "needs_manual_review" or \
            original_transcript_status != "needs_manual_review"
        if recheck_required and shared.get("confirm_reset_review") is not True:
            raise Conflict("Paired question is already reviewed. Explicitly confirm reverting its status to needs_manual_review, while preserving past approvals")
        if self._version(db, sibling_id) != shared["other_version"]:
            raise Conflict("The paired question changed in another tab. Reload both questions before shared correction")
        if new_text == first["dialogue_text"]:
            raise ReviewError("Shared transcript confirmation is only for an actual transcript correction")
        return {"other_question_id": sibling_id,
                "old_text": by_id[sibling_id]["dialogue_text"],
                "other_version": shared["other_version"],
                "previous_question_status": original_status,
                "previous_transcript_status": original_transcript_status,
                "explicit_recheck": recheck_required,
                "source_file_id": first["source_file_id"],
                "source_pdf_page": first["source_pdf_page"]}

    def save_review(self, question_id: str, payload: dict, *, fast_response: bool = False) -> dict:
        self._list_questions_cache = None
        with self._review_write_transaction(question_id) as db:
            # Explicit two-question corrections must lock in the same fixed
            # order as shared-audio writes. Never first hold the requested
            # question lock and then wait on the sibling (reverse order).
            requested_shared = payload.get("shared_transcript") if isinstance(payload, dict) else None
            if self.exam_id == DEFAULT_EXAM_ID and \
                    isinstance(question_id, str) and QUESTION_ID.fullmatch(question_id):
                try:
                    number = int(question_id.rsplit("-", 1)[-1])
                except ValueError:
                    number = -1
                pair = SHARED_AUDIO.get(number)
                if pair:
                    ids = sorted(f"035-I-L-{n:03d}" for n in pair)
                    if self.backend == "postgres":
                        locked = db.execute(
                            "SELECT id FROM questions WHERE id IN (?,?) ORDER BY id FOR UPDATE", ids
                        ).fetchall()
                        if len(locked) != 2:
                            raise Conflict("Paired questions are missing; shared transcript cannot be changed")
            question = self._lock_question(db, question_id)
            old_choices = [row["text"] for row in db.execute(
                "SELECT text FROM choices WHERE question_id=? ORDER BY number", (question_id,)
            )]
            transcript_row = db.execute(
                "SELECT dialogue_text FROM transcripts WHERE question_id=?", (question_id,)
            ).fetchone()
            old_transcript = transcript_row["dialogue_text"] if transcript_row else None
            existing = {"transcript": transcript_row is not None}
            payload = self._validate_payload(payload, question, existing)
            version = self._version(db, question_id)
            if version != payload["version"]:
                raise Conflict("This question was saved in another tab. Reload before editing again.")
            if payload["status"] == "verified" and question["section"] == "listening" and \
                    self.exam_id == DEFAULT_EXAM_ID and question["exam_number"] in SHARED_AUDIO and \
                    requested_shared is None:
                shared_state = self._shared_transcript_state(db, question["exam_number"])
                if not shared_state or not shared_state["consistent"]:
                    raise Conflict("Shared transcript sources differ. Do not approve until the paired dialogue is reconciled")
            correction = None
            if old_transcript != payload["transcript_text"] and \
                    self.exam_id == DEFAULT_EXAM_ID and question["section"] == "listening" and \
                    question["exam_number"] in SHARED_AUDIO:
                correction = self._locked_shared_transcripts(
                    db, question_id, question["exam_number"], payload["transcript_text"],
                    payload.get("shared_transcript"),
                )
            elif payload.get("shared_transcript") is not None:
                raise ReviewError("Shared correction confirmation requires changing a confirmed 35th shared transcript")
            old = {"stem": question["stem"], "choices": old_choices,
                   "transcript_text": old_transcript, "status": question["review_status"]}
            new = {"stem": payload["stem"], "choices": payload["choices"],
                   "transcript_text": payload["transcript_text"], "status": payload["status"]}
            saved = new != old or bool(payload["note"].strip())
            if saved:
                db.execute("UPDATE questions SET stem=?, review_status=? WHERE id=?",
                           (new["stem"], new["status"], question_id))
                changed_choices = [(text, question_id, n) for n, text in enumerate(new["choices"], 1)
                                   if text != old_choices[n - 1]]
                if changed_choices:
                    db.executemany("UPDATE choices SET text=? WHERE question_id=? AND number=?",
                                   changed_choices)
                if transcript_row:
                    db.execute("UPDATE transcripts SET dialogue_text=?, review_status=? WHERE question_id=?",
                               (new["transcript_text"], new["status"], question_id))
                if correction:
                    other = correction["other_question_id"]
                    # Review decisions are independent. Source revisions make
                    # older sibling approvals stale ONLY after explicit
                    # reviewer consent; historical approvals remain append-only.
                    db.execute("UPDATE transcripts SET dialogue_text=?,review_status='needs_manual_review' "
                               "WHERE question_id=?", (new["transcript_text"], other))
                    if correction["explicit_recheck"]:
                        db.execute("UPDATE questions SET review_status='needs_manual_review' WHERE id=?", (other,))
                    db.execute(
                        "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        ("question", other, "needs_manual_review", "local_reviewer",
                         "shared_transcript_source_correction",
                         json.dumps({"paired_question_id": question_id,
                                     "before": correction["old_text"],
                                     "after": new["transcript_text"],
                                     "source_file_id": correction["source_file_id"],
                                     "source_pdf_page": correction["source_pdf_page"],
                                     "previous_question_status": correction["previous_question_status"],
                                     "previous_transcript_status": correction["previous_transcript_status"],
                                     "human_approval_changed": correction["explicit_recheck"],
                                     "historical_approvals_preserved": True,
                                     "explicit_pair_recheck": correction["explicit_recheck"]}, ensure_ascii=False),
                         datetime.now(timezone.utc).isoformat(timespec="seconds")),
                    )
                db.execute(
                    "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    ("question", question_id, new["status"], "local_reviewer", "manual_question_review",
                     json.dumps({"before": old, "after": new, "note": payload["note"]}, ensure_ascii=False),
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
            last_human = None
            human_evidence = None
            history = None
            if fast_response:
                # The exact committed snapshot, not a subsequent detail GET,
                # must drive the optimistic UI's approval and visible history.
                records = db.execute(
                    "SELECT id,status,scope,reviewer,evidence,reviewed_at "
                    "FROM review_records WHERE subject_type='question' AND subject_id=? "
                    "ORDER BY id DESC LIMIT 30", (question_id,),
                ).fetchall()
                history = [self._question_history_item(r) for r in records]
                manual = next((r for r in records if r["scope"] == "manual_question_review"), None)
                if manual is None and len(records) == 30:
                    manual = db.execute(
                        "SELECT id,status,reviewer,reviewed_at FROM review_records "
                        "WHERE subject_type='question' AND subject_id=? "
                        "AND scope='manual_question_review' ORDER BY id DESC LIMIT 1",
                        (question_id,),
                    ).fetchone()
                last_human, human_evidence = self._question_human_evidence(
                    new["status"], records[0] if records else None, manual)
        if fast_response:
            # The transaction has committed. This exact version is authoritative;
            # a second full detail read is unnecessary for ordinary navigation.
            final_version = version + int(saved)
            return {"id": question_id, "number": question["exam_number"],
                    "review_status": new["status"], "version": final_version,
                    "request_version": payload["version"], "saved_version": final_version,
                    "last_human_review": last_human,
                    "human_review_evidence": human_evidence,
                    "history": history,
                    "saved_review_event": history[0] if saved and history else None,
                    "saved_review_id": last_human["id"] if saved and last_human else None,
                    "saved_reviewed_at": last_human["reviewed_at"] if saved and last_human else None,
                    "requires_image": bool(question["requires_image"]), "saved": saved,
                    "shared_transcript_updated": ({"id": correction["other_question_id"],
                        "version": correction["other_version"] + 1,
                        "review_status": "needs_manual_review"} if correction else None)}
        return self.get_question(question_id)

    def media_path(self, question_id: str, kind: str) -> Path:
        if kind not in ("paper", "answer", "transcript", "audio"):
            raise NotFound("Unsupported media")
        with closing(self._connect()) as db:
            question = self._question(db, question_id)
            return self._media_path_with_connection(db, question, kind)

    def _media_path_with_connection(self, db, question, kind: str) -> Path:
        """Validate media on a caller's existing read snapshot to avoid reconnects."""
        if kind not in ("paper", "answer", "transcript", "audio"):
            raise NotFound("Unsupported media")
        if kind == "paper":
            source_id = question["source_file_id"]
        elif kind == "answer":
            source_id = question["answer_file_id"]
        elif kind == "transcript":
            record = db.execute("SELECT source_file_id FROM transcripts WHERE question_id=?",
                                (question["id"],)).fetchone()
            source_id = record["source_file_id"] if record else None
        else:
            record = db.execute("SELECT source_file_id FROM audio_assets WHERE section_id=?",
                                (question["section_id"],)).fetchone()
            source_id = record["source_file_id"] if record else None
        if source_id is None:
            raise NotFound("This question has no such source")
        record = db.execute("SELECT relative_path,sha256,byte_size FROM source_files WHERE id=?",
                            (source_id,)).fetchone()
        if record is None:
            raise NotFound("Source does not exist")
        return self._verify_media_source(record["relative_path"], record["sha256"],
                                         record["byte_size"], kind)

    def _verify_media_source(self, relative: str, sha256: str, byte_size: int, kind: str) -> Path:
        """Check one local original against DB metadata, with no integrity shortcuts."""
        if not relative or "\\" in relative or Path(relative).is_absolute():
            raise ReviewError("Unsafe stored source path")
        source = self._local_media_path(relative)
        try:
            source.relative_to(self.source_root)
        except ValueError as exc:
            raise ReviewError("Source escapes the selected exam folder") from exc
        if not source.is_file() or source.suffix.lower() != (".mp3" if kind == "audio" else ".pdf"):
            raise NotFound("Source file unavailable or of unexpected type")
        if source.stat().st_size != byte_size:
            raise Conflict("Original source file size changed; review is blocked")
        checksum = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        if checksum.hexdigest() != sha256:
            raise Conflict("Original source file checksum changed; review is blocked")
        return source

    def _local_media_path(self, relative: str) -> Path:
        """Resolve DB logical paths against the device-local TOPIK_MEDIA_ROOT."""
        logical = Path(relative)
        parts = logical.parts
        if parts and parts[0] == "topik-past-papers":
            logical = Path(*parts[1:])
        return (self.media_root / logical).resolve()

    def get_image(self, question_id: str, index: int) -> tuple[bytes, str]:
        if type(index) is not int or not 0 <= index < 100:
            raise NotFound("Image index is invalid")
        with closing(self._connect()) as db:
            self._question(db, question_id)
            result = db.execute(
                "SELECT i.bytes, i.mime_type FROM question_images qi JOIN images i ON i.key=qi.image_key "
                "WHERE qi.question_id=? ORDER BY qi.image_key LIMIT 1 OFFSET ?", (question_id, index),
            ).fetchone()
            if result is None:
                raise NotFound("Image unavailable")
            payload = bytes(result["bytes"])
            if result["mime_type"] != "image/png" or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
                raise NotFound("Image unavailable")
            return payload, result["mime_type"]


def make_handler(store: ReviewStore, *, access_key: str | None,
                 allow_unauthenticated_test_fixture: bool = False):
    """Build a loopback HTTP handler with per-process operator-key protection.

    The explicitly insecure test-fixture switch exists for legacy integration
    tests only; the real launcher and ordinary calls never enable it. An
    operator key authorizes a browser session, NOT a verified human identity.
    """
    if allow_unauthenticated_test_fixture:
        if access_key is not None:
            raise ValueError("Test fixture mode must not specify an access key")
    else:
        if access_key is None:
            raise ValueError("Operator access key must be explicitly supplied")
        if not isinstance(access_key, str) or len(access_key) < 32:
            raise ValueError("Operator access key must have at least 32 characters")
    basic_header = ("Basic " + base64.b64encode(
        f"operator:{access_key}".encode("utf-8")).decode("ascii")
        if not allow_unauthenticated_test_fixture else None)
    basic_header_bytes = basic_header.encode("ascii") if basic_header is not None else None
    csrf_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "TOPIKLocalReview/1.0"

        def _request_store(self):
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            selected = query.get("exam_id", [])
            if not selected:
                return store
            if len(selected) != 1 or not EXAM_ID.fullmatch(selected[0]):
                raise ReviewError("Invalid exam ID")
            return store.for_exam(selected[0])

        def _headers(self, status: int, mime: str, length: int, **extra):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; "
                             "media-src 'self'; frame-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; form-action 'self'")
            for name, value in extra.items():
                self.send_header(name.replace("_", "-"), str(value))
            self.end_headers()

        def _drain_body(self):
            if not getattr(self, "_body_consumed", False):
                self._body_consumed = True
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if 0 < length <= MAX_POST_BYTES:
                        self.rfile.read(length)
                except Exception:
                    pass

        def _json(self, status: int, content: dict):
            self._drain_body()
            payload = json.dumps(content, ensure_ascii=False).encode("utf-8")
            extra = {"Vary": "Accept-Encoding"}
            # The real 35th F3 bundle is ~200 KiB of repeated JSON keys/text.
            # Negotiate transport compression without changing the JSON contract,
            # audit provenance, database snapshot or client request sequence.
            # Do not gzip tiny responses, unsupported clients, or incompressible
            # payloads; browser fetch transparently decodes this representation.
            if len(payload) >= 2048 and self._accepts_gzip():
                compressed = gzip.compress(payload, compresslevel=5, mtime=0)
                if len(compressed) + 128 < len(payload):
                    payload = compressed
                    extra["Content_Encoding"] = "gzip"
            self._headers(status, "application/json; charset=utf-8", len(payload), **extra)
            self.wfile.write(payload)

        def _accepts_gzip(self) -> bool:
            # An explicit `gzip;q=0` must never be overridden by a wildcard.
            for component in self.headers.get("Accept-Encoding", "").split(","):
                coding, *parameters = component.split(";")
                if coding.strip().lower() != "gzip":
                    continue
                for parameter in parameters:
                    name, separator, value = parameter.partition("=")
                    if name.strip().lower() == "q":
                        try:
                            return separator == "=" and 0.0 < float(value.strip()) <= 1.0
                        except ValueError:
                            return False
                return True
            return False

        def _origin(self) -> str:
            return f"http://127.0.0.1:{self.server.server_port}"

        def _host_valid(self) -> bool:
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def _operator_access_valid(self) -> bool:
            if basic_header is None:
                return True  # Explicit private, disposable HTTP test fixture.
            supplied = self.headers.get("Authorization", "")
            return isinstance(supplied, str) and secrets.compare_digest(
                supplied.encode("utf-8"), basic_header_bytes)

        def _operator_required(self):
            self._drain_body()
            payload = b'{"error":"Operator access required"}'
            self._headers(401, "application/json; charset=utf-8", len(payload),
                          WWW_Authenticate='Basic realm="TOPIK local operator", charset="UTF-8"')
            self.wfile.write(payload)

        def _error(self, exc: Exception):
            code = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict) else 400
            self._json(code, {"error": str(exc)})

        def _file(self, path: Path):
            size = path.stat().st_size
            start, end, status = 0, size - 1, 200
            range_header = self.headers.get("Range")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                if not match or not any(match.groups()) or size == 0:
                    return self._range_error(size)
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), end) if last else end
                else:
                    start = max(0, size - int(last))
                if start >= size or end < start or (not first and int(last) == 0):
                    return self._range_error(size)
                status = 206
            length = end - start + 1
            mime = "application/pdf" if path.suffix.lower() == ".pdf" else "audio/mpeg"
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.end_headers()
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        break
                    try:
                        self.wfile.write(block)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        break
                    remaining -= len(block)

        def _range_error(self, size: int):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self._host_valid():
                return self._json(403, {"error": "Only 127.0.0.1 is allowed"})
            if not self._operator_access_valid():
                return self._operator_required()
            parts = [unquote(piece) for piece in urlsplit(self.path).path.split("/") if piece]
            try:
                active_store = self._request_store()
                if not parts:
                    contents = HTML_PATH.read_bytes()
                    self._headers(200, "text/html; charset=utf-8", len(contents))
                    return self.wfile.write(contents)
                if parts == ["api", "questions"]:
                    return self._json(200, {**active_store.list_questions(), "csrf_token": csrf_token})
                if parts == ["api", "questions-fast"]:
                    return self._json(200, {**active_store.list_questions_fast(), "csrf_token": csrf_token})
                if parts == ["api", "questions-ai-summary"]:
                    return self._json(200, active_store.list_ai_audit_summary())
                if parts == ["api", "questions-bundle"]:
                    return self._json(200, active_store.get_questions_bundle())
                if parts == ["api", "independent-audits"]:
                    return self._json(200, active_store.get_independent_audit_comparison())
                if len(parts) == 3 and parts[:2] == ["api", "questions"]:
                    fast = parse_qs(urlsplit(self.path).query).get("fast") == ["1"]
                    return self._json(200, active_store.get_question(parts[2], fast=fast))
                if len(parts) == 3 and parts[0] == "media":
                    if parts[2] == "clip":
                        return self._file(active_store.clip_path(parts[1]))
                    return self._file(active_store.media_path(parts[1], parts[2]))
                if len(parts) == 4 and parts[0] == "media" and parts[2] == "image" and parts[3].isdigit():
                    image, mime = active_store.get_image(parts[1], int(parts[3]))
                    self._headers(200, mime, len(image))
                    return self.wfile.write(image)
                raise NotFound("Unknown page")
            except ReviewError as exc:
                return self._error(exc)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # A browser can cancel an in-flight image/PDF navigation after
                # the successful response headers have already been written.
                # Never attempt to send a second 500 on that closed socket.
                return
            except (OSError, sqlite3.Error, DatabaseConfigError, DatabaseOperationError):
                return self._json(500, {"error": "Local file or database unavailable"})

        def do_POST(self):
            if not self._host_valid():
                return self._json(403, {"error": "Only 127.0.0.1 is allowed"})
            if not self._operator_access_valid():
                return self._operator_required()
            if self.headers.get("Origin") != self._origin():
                return self._json(403, {"error": "Cross-origin requests are not allowed"})
            if not secrets.compare_digest(self.headers.get("X-Review-Token", ""), csrf_token):
                return self._json(403, {"error": "Invalid review token; reload the page"})
            parts = [unquote(piece) for piece in urlsplit(self.path).path.split("/") if piece]
            if len(parts) != 4 or parts[:2] != ["api", "questions"] or parts[3] not in (
                    "review", "audio-segment", "export-clip"):
                return self._json(404, {"error": "Unknown endpoint"})
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                return self._json(415, {"error": "Expected application/json"})
            try:
                active_store = self._request_store()
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_POST_BYTES:
                    raise ReviewError("Review request too large or empty")
                self._body_consumed = True
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if parts[3] == "review":
                    result = active_store.save_review(parts[2], payload,
                        fast_response=parse_qs(urlsplit(self.path).query).get("fast") == ["1"])
                elif parts[3] == "audio-segment":
                    result = active_store.save_audio_segment(parts[2], payload)
                else:
                    if payload != {}:
                        raise ReviewError("Audio export request must be an empty JSON object")
                    result = active_store.export_audio_clip(parts[2])
                return self._json(200, result)
            except ReviewError as exc:
                return self._error(exc)
            except (ValueError, UnicodeError) as exc:
                return self._json(400, {"error": str(exc)})
            except (sqlite3.Error, DatabaseConfigError, DatabaseOperationError):
                return self._json(500, {"error": "Local database unavailable"})

        def log_message(self, fmt, *args):
            # Avoid printing source text, request bodies and local paths.
            return

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=None,
                        help="Local-only port (default: try 8765, then choose a free port; 0: OS-selected)")
    parser.add_argument("--exam-id", default=DEFAULT_EXAM_ID,
                        help="Explicit exam selection, e.g. 035-I-B or 036-I-B (default: 035-I-B)")
    args = parser.parse_args()
    if args.port is not None and not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    store = ReviewStore(exam_id=args.exam_id)
    operator_key = secrets.token_urlsafe(32)
    handler = make_handler(store, access_key=operator_key)
    requested = 8765 if args.port is None else args.port
    try:
        server = ThreadingHTTPServer(("127.0.0.1", requested), handler)
    except OSError as error:
        # On Windows an occupied port can raise WinError 10013 rather than
        # 10048. Only the *default* may fall back; an explicit port is strict.
        conflict = error.errno in (errno.EACCES, errno.EADDRINUSE) or getattr(error, "winerror", None) in (10013, 10048)
        if args.port is not None or not conflict:
            raise SystemExit(f"Cannot bind 127.0.0.1:{requested}: {error}. "
                             "Try --port 0 to select a free local port.") from error
        print(f"Local port {requested} is unavailable; selecting a free port instead.", file=sys.stderr)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except OSError as fallback_error:
            raise SystemExit(f"Cannot bind a local review server: {fallback_error}") from fallback_error
    with server:
        print(f"TOPIK {getattr(store, 'exam_id', DEFAULT_EXAM_ID)} review: http://127.0.0.1:{server.server_port}/")
        print("Operator access: username operator")
        print(f"Operator access: password {operator_key}")
        print("The browser will request this per-launch password. Keep it private; it proves possession only, not reviewer identity.")
        if getattr(store, "backend", "sqlite") == "postgres":
            print("Central PostgreSQL review mode. Source PDFs/audio and exported clips stay device-local; review/audio/clip workflows are enabled.")
        else:
            print("Explicit legacy SQLite fixture mode. The canonical Stage 10 SQLite archive cannot be edited.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Stopped.")


if __name__ == "__main__":
    main()
