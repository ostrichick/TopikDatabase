"""Local-only review screen for the 35th TOPIK I pilot database.

Run from the project root: py -3 src/review_ui.py
Open the local URL printed in the terminal (port 8765 or an available fallback).
No third-party web services are used.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import inspect
import json
import os
import re
import secrets
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
HTML_PATH = Path(__file__).with_name("review_ui.html")
QUESTION_ID = re.compile(r"^035-I-[LR]-\d{3}$")
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
    def __init__(self, db_path: Path = DB_PATH, root: Path = ROOT):
        self.db_path = Path(db_path).resolve()
        self.root = Path(root).resolve()
        self.source_root = (self.root / "topik-past-papers" / "35th").resolve()
        if not self.db_path.is_file():
            raise FileNotFoundError(f"Pilot database not found: {self.db_path}. Run py -3 src/pilot_35.py first.")
        with closing(self._connect()) as db:
            exam = db.execute("SELECT session, level, booklet FROM exams").fetchall()
            if len(exam) != 1 or tuple(exam[0]) != (35, "I", "B"):
                raise ReviewError("This reviewer only accepts the 35th TOPIK I B pilot database")

    def _connect(self, writable: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                                     uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _question(db: sqlite3.Connection, question_id: str) -> sqlite3.Row:
        if not isinstance(question_id, str) or not QUESTION_ID.fullmatch(question_id):
            raise NotFound("Unknown question")
        row = db.execute(
            "SELECT q.*, s.name AS section, g.instruction, g.passage_text, "
            "g.first_exam_number, g.last_exam_number, a.choice_number, "
            "a.source_file_id AS answer_file_id, a.source_pdf_page AS answer_pdf_page "
            "FROM questions q JOIN sections s ON s.id=q.section_id "
            "LEFT JOIN question_groups g ON g.id=q.group_id "
            "JOIN answers a ON a.question_id=q.id WHERE q.id=?", (question_id,)
        ).fetchone()
        if row is None:
            raise NotFound("Unknown question")
        return row

    @staticmethod
    def _version(db: sqlite3.Connection, question_id: str) -> int:
        return db.execute(
            "SELECT COUNT(*) FROM review_records WHERE subject_type='question' "
            "AND subject_id=?",
            (question_id,)
        ).fetchone()[0]

    def list_questions(self) -> dict:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT q.id, q.exam_number AS number, s.name AS section, "
                "q.review_status AS status, q.requires_image "
                "FROM questions q JOIN sections s ON s.id=q.section_id ORDER BY q.exam_number"
            ).fetchall()
            items = [dict(row) for row in rows]
            ai_available, ai_summaries = self._ai_audit_summaries(db)
            if ai_available:
                for item in items:
                    summary = ai_summaries.get(item["id"])
                    audit = self._ai_audit_list_summary(summary) if summary else {}
                    run_id = summary.get("run_id") if summary else None
                    execution = self._ai_audit_execution(db, subject_id=item["id"], run_id=run_id)
                    execution_summary = self._ai_audit_execution_list_summary(execution)
                    if execution_summary:
                        audit.update(execution_summary)
                    if audit:
                        item["ai_audit"] = audit
            counts = {status: sum(item["status"] == status for item in items) for status in STATUSES}
            counts["total"] = len(items)
            return {"items": items, "counts": counts, "ai_audit_available": ai_available}

    @staticmethod
    def _has_ai_audit_tables(db: sqlite3.Connection) -> bool:
        existing = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ai_audit_%'"
            )
        }
        return AI_AUDIT_TABLES.issubset(existing)

    @classmethod
    def _ai_audit_summaries(cls, db: sqlite3.Connection) -> tuple[bool, dict]:
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
    def _ai_audit_summary(cls, db: sqlite3.Connection, question_id: str) -> dict | None:
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
    def _ai_audit_history(db: sqlite3.Connection, question_id: str) -> dict | None:
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

    @staticmethod
    def _ai_audit_table_names(db: sqlite3.Connection) -> set[str]:
        return {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ai_audit_%'"
            )
        }

    @classmethod
    def _ai_audit_execution(
        cls,
        db: sqlite3.Connection,
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

    @staticmethod
    def _has_audio_segments(db: sqlite3.Connection) -> bool:
        return db.execute("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='audio_segments')").fetchone()[0] == 1

    def _audio_info(self, db: sqlite3.Connection, question: sqlite3.Row) -> dict | None:
        if question["section"] != "listening" or not self._has_audio_segments(db):
            return None
        asset = db.execute(
            "SELECT a.id,a.duration_seconds,s.sha256 FROM audio_assets a "
            "JOIN source_files s ON s.id=a.source_file_id WHERE a.section_id=?",
            (question["section_id"],),
        ).fetchone()
        if not asset or asset["duration_seconds"] is None:
            return None
        segment = db.execute("SELECT * FROM audio_segments WHERE question_id=?",
                             (question["id"],)).fetchone()
        if segment is None:
            return None
        pair = list(SHARED_AUDIO.get(question["exam_number"], (question["exam_number"],)))
        clip_url = None
        if segment["status"] == "verified" and segment["clip_relative_path"]:
            # Never reveal an unverified file path supplied by database contents.
            try:
                self._clip_file(segment)
                clip_url = f"/media/{question['id']}/clip"
            except ReviewError:
                pass
        return {"start_ms": segment["start_ms"], "end_ms": segment["end_ms"],
                "status": segment["status"], "version": segment["version"],
                "source_duration_ms": round(asset["duration_seconds"] * 1000),
                "shared_questions": pair, "clip_url": clip_url}

    def _clip_file(self, segment: sqlite3.Row) -> Path:
        relative = segment["clip_relative_path"]
        expected_root = (self.root / "topik-past-papers" / "derived" / "audio-clips").resolve()
        if not relative or not isinstance(relative, str) or "\\" in relative:
            raise NotFound("No exported clip")
        path = (self.root / relative).resolve()
        if path.parent != expected_root or path.suffix.lower() != ".mp3" or not path.is_file():
            raise NotFound("Exported clip unavailable")
        if hashlib.sha256(path.read_bytes()).hexdigest() != segment["clip_sha256"]:
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
            return self._clip_file(segment)

    def save_audio_segment(self, question_id: str, payload: dict) -> dict:
        """Persist a reviewer-specified interval; sync shared-dialogue pairs atomically.

        This NEVER modifies question/text approval or silently verifies the audio.
        """
        if not isinstance(payload, dict) or set(payload) != {"version", "start_ms", "end_ms", "status"}:
            raise ReviewError("Expected version, start_ms, end_ms and status only")
        version, start, end, status = (payload[k] for k in ("version", "start_ms", "end_ms", "status"))
        if type(version) is not int or version < 0 or type(start) is not int or type(end) is not int:
            raise ReviewError("Audio boundaries and version must be integer milliseconds")
        if status not in ("candidate", "verified") or not 0 <= start < end or end - start < 500:
            raise ReviewError("Audio segment must have a valid status and be at least 0.5 seconds long")
        with closing(self._connect(writable=True)) as db:
            db.execute("BEGIN IMMEDIATE")
            question = self._question(db, question_id)
            if question["section"] != "listening" or not self._has_audio_segments(db):
                raise ReviewError("Audio segmentation is available only after setup for listening questions")
            asset = db.execute(
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
            qids = [f"035-I-L-{number:03d}" for number in pair]
            rows = {item["question_id"]: item for item in db.execute(
                f"SELECT * FROM audio_segments WHERE question_id IN ({','.join('?' for _ in qids)})", qids,
            )}
            if any((rows[qid]["version"] if qid in rows else 0) != version for qid in qids):
                raise Conflict("Audio interval was changed in another tab; reload before saving")
            if status == "verified" and any(qid not in rows for qid in qids):
                raise ReviewError("Preview and save a candidate before verifying its interval")
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
                    (qid, asset["id"], start, end, status, 1, asset["sha256"], now),
                )
                db.execute("INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                           "VALUES(?,?,?,?,?,?,?)",
                           ("audio_segment", qid, status, "local_reviewer", "manual_audio_boundary_35",
                            json.dumps({"before": prior, "after": after, "source_sha256": asset["sha256"]},
                                       ensure_ascii=False), now))
            db.commit()
        return self.get_question(question_id)

    def export_audio_clip(self, question_id: str) -> dict:
        """Export only a manually verified interval, preserving MP3 source unchanged."""
        from src.audio_35 import export_segment

        with closing(self._connect()) as db:
            question = self._question(db, question_id)
            if question["section"] != "listening" or not self._has_audio_segments(db):
                raise ReviewError("Only reviewed listening intervals can be exported")
            pair = SHARED_AUDIO.get(question["exam_number"], (question["exam_number"],))
            qids = [f"035-I-L-{number:03d}" for number in pair]
            rows = [db.execute("SELECT * FROM audio_segments WHERE question_id=?", (qid,)).fetchone()
                    for qid in qids]
            if any(row is None or row["status"] != "verified" for row in rows):
                raise ReviewError("Verify every linked interval before exporting an MP3")
            expected = (rows[0]["start_ms"], rows[0]["end_ms"], rows[0]["version"], rows[0]["source_sha256"])
            if any((row["start_ms"], row["end_ms"], row["version"], row["source_sha256"]) != expected
                   for row in rows):
                raise Conflict("Shared dialogue bounds differ; reload and reconcile")
            if rows[0]["clip_relative_path"]:
                return self.get_question(question_id)
            start, end, version, expected_sha = expected
            audio_source = db.execute(
                "SELECT s.sha256,s.byte_size FROM audio_assets a JOIN source_files s "
                "ON s.id=a.source_file_id WHERE a.id=?", (rows[0]["audio_asset_id"],)
            ).fetchone()
            if audio_source is None or audio_source["sha256"] != expected_sha:
                raise ReviewError("The segment's original audio checksum no longer matches")

        source = self.media_path(question_id, "audio")
        if source.stat().st_size != audio_source["byte_size"] or hashlib.sha256(source.read_bytes()).hexdigest() != expected_sha:
            raise ReviewError("The original recording changed; export stopped")
        output_dir = self.root / "topik-past-papers" / "derived" / "audio-clips"
        output_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=".audio-export-", suffix=".mp3", dir=output_dir)
        os.close(descriptor)
        temporary = Path(temp_name)
        # export_segment deliberately refuses to overwrite ANY existing file.
        # Reserve a unique name above, then remove only our own empty placeholder.
        temporary.unlink()
        published: Path | None = None
        try:
            export_segment(source, temporary, start, end)
            if not temporary.is_file() or temporary.stat().st_size < 1000:
                raise ReviewError("FFmpeg produced an empty or invalid clip")
            sha = hashlib.sha256(temporary.read_bytes()).hexdigest()
            name = f"035-I-L-{'-'.join(f'{n:03d}' for n in pair)}-v{version}-{sha[:12]}.mp3"
            destination = (output_dir / name).resolve()
            if destination.parent != output_dir.resolve():
                raise ReviewError("Unsafe export destination")
            relative = destination.relative_to(self.root).as_posix()
            with closing(self._connect(writable=True)) as db:
                db.execute("BEGIN IMMEDIATE")
                current = [db.execute("SELECT * FROM audio_segments WHERE question_id=?", (qid,)).fetchone()
                           for qid in qids]
                if any(row is None or row["status"] != "verified" or
                       (row["start_ms"], row["end_ms"], row["version"], row["source_sha256"]) != expected or
                       row["clip_relative_path"] for row in current):
                    raise Conflict("Audio interval changed while exporting; no clip was linked")
                if destination.exists():
                    if hashlib.sha256(destination.read_bytes()).hexdigest() != sha:
                        raise Conflict("Export destination already contains different audio")
                else:
                    os.replace(temporary, destination)
                    published = destination
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for qid in qids:
                    db.execute("UPDATE audio_segments SET clip_relative_path=?,clip_sha256=? WHERE question_id=?",
                               (relative, sha, qid))
                    db.execute("INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                               "VALUES(?,?,?,?,?,?,?)", ("audio_segment", qid, "verified", "local_reviewer",
                               "audio_export_35", json.dumps({"clip": relative, "sha256": sha,
                                                              "source_sha256": expected_sha, "start_ms": start,
                                                              "end_ms": end}), now))
                db.commit()
        except BaseException:
            if published is not None:
                published.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        return self.get_question(question_id)

    def get_question(self, question_id: str) -> dict:
        with closing(self._connect()) as db:
            row = self._question(db, question_id)
            question = dict(row)
            ai_audit = self._ai_audit_summary(db, question_id)
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
            image_keys = [item[0] for item in db.execute(
                "SELECT image_key FROM question_images WHERE question_id=? ORDER BY image_key", (question_id,)
            )]
            history = []
            for record in db.execute(
                "SELECT status, scope, evidence, reviewed_at FROM review_records "
                "WHERE subject_type='question' AND subject_id=? ORDER BY id DESC LIMIT 30", (question_id,)
            ):
                item = dict(record)
                try:
                    item["note"] = json.loads(item["evidence"]).get("note", "")
                except (ValueError, TypeError):
                    item["note"] = ""
                item.pop("evidence")
                history.append(item)
            result = {
                "id": question_id,
                "number": question["exam_number"],
                "section": question["section"],
                "review_status": question["review_status"],
                "version": self._version(db, question_id),
                "stem": question["stem"],
                "raw_question_text": question["raw_question_text"],
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
                "source_pdf_url": f"/media/{question_id}/paper#page={question['source_pdf_page']}",
                "answer_pdf_url": f"/media/{question_id}/answer#page={question['answer_pdf_page']}",
                "transcript": transcript_info,
                "transcript_pdf_url": (f"/media/{question_id}/transcript#page={transcript_info['source_pdf_page']}"
                                       if transcript_info else None),
                "audio_url": f"/media/{question_id}/audio" if transcript_info else None,
                "audio_segment": self._audio_info(db, row),
                "images": [{"url": f"/media/{question_id}/image/{index}", "key": key}
                           for index, key in enumerate(image_keys)],
                "requires_image": bool(question["requires_image"]),
                "preview_flags": json.loads(question["preview_flags_json"]),
                "history": history,
            }
            if ai_audit:
                result["ai_audit"] = ai_audit
            return result

    @staticmethod
    def _validate_payload(payload: dict, question: sqlite3.Row, existing: dict) -> dict:
        if not isinstance(payload, dict):
            raise ReviewError("Expected JSON object")
        allowed = {"version", "status", "stem", "choices", "transcript_text", "note"}
        if set(payload) != allowed:
            raise ReviewError("Review payload must contain only version, status, stem, choices, transcript_text and note")
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
        return payload

    def save_review(self, question_id: str, payload: dict) -> dict:
        with closing(self._connect(writable=True)) as db:
            db.execute("BEGIN IMMEDIATE")
            question = self._question(db, question_id)
            # A reviewer must be comparing the same originals whose hashes
            # were recorded during import, including the independent answer key.
            for kind in (("paper", "answer", "transcript") if question["section"] == "listening"
                         else ("paper", "answer")):
                self.media_path(question_id, kind)
            old_choices = [row[0] for row in db.execute(
                "SELECT text FROM choices WHERE question_id=? ORDER BY number", (question_id,)
            )]
            transcript_row = db.execute(
                "SELECT dialogue_text FROM transcripts WHERE question_id=?", (question_id,)
            ).fetchone()
            old_transcript = transcript_row[0] if transcript_row else None
            existing = {"transcript": transcript_row is not None}
            payload = self._validate_payload(payload, question, existing)
            version = self._version(db, question_id)
            if version != payload["version"]:
                raise Conflict("This question was saved in another tab. Reload before editing again.")
            old = {"stem": question["stem"], "choices": old_choices,
                   "transcript_text": old_transcript, "status": question["review_status"]}
            new = {"stem": payload["stem"], "choices": payload["choices"],
                   "transcript_text": payload["transcript_text"], "status": payload["status"]}
            if new != old or payload["note"].strip():
                db.execute("UPDATE questions SET stem=?, review_status=? WHERE id=?",
                           (new["stem"], new["status"], question_id))
                db.executemany("UPDATE choices SET text=? WHERE question_id=? AND number=?",
                               [(text, question_id, n) for n, text in enumerate(new["choices"], 1)])
                if transcript_row:
                    db.execute("UPDATE transcripts SET dialogue_text=?, review_status=? WHERE question_id=?",
                               (new["transcript_text"], new["status"], question_id))
                db.execute(
                    "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    ("question", question_id, new["status"], "local_reviewer", "manual_question_review",
                     json.dumps({"before": old, "after": new, "note": payload["note"]}, ensure_ascii=False),
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
            db.commit()
        return self.get_question(question_id)

    def media_path(self, question_id: str, kind: str) -> Path:
        if kind not in ("paper", "answer", "transcript", "audio"):
            raise NotFound("Unsupported media")
        with closing(self._connect()) as db:
            question = self._question(db, question_id)
            if kind == "paper":
                source_id = question["source_file_id"]
            elif kind == "answer":
                source_id = question["answer_file_id"]
            elif kind == "transcript":
                record = db.execute("SELECT source_file_id FROM transcripts WHERE question_id=?",
                                    (question_id,)).fetchone()
                source_id = record[0] if record else None
            else:
                record = db.execute("SELECT source_file_id FROM audio_assets WHERE section_id=?",
                                    (question["section_id"],)).fetchone()
                source_id = record[0] if record else None
            if source_id is None:
                raise NotFound("This question has no such source")
            record = db.execute("SELECT relative_path,sha256,byte_size FROM source_files WHERE id=?",
                                (source_id,)).fetchone()
            if record is None:
                raise NotFound("Source does not exist")
            relative = record["relative_path"]
            if not relative or "\\" in relative or Path(relative).is_absolute():
                raise ReviewError("Unsafe stored source path")
            source = (self.root / relative).resolve()
            try:
                source.relative_to(self.source_root)
            except ValueError as exc:
                raise ReviewError("Source escapes the approved 35th folder") from exc
            if not source.is_file() or source.suffix.lower() != (".mp3" if kind == "audio" else ".pdf"):
                raise NotFound("Source file unavailable or of unexpected type")
            if source.stat().st_size != record["byte_size"]:
                raise Conflict("Original source file size changed; review is blocked")
            checksum = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    checksum.update(chunk)
            if checksum.hexdigest() != record["sha256"]:
                raise Conflict("Original source file checksum changed; review is blocked")
            return source

    def get_image(self, question_id: str, index: int) -> tuple[bytes, str]:
        if type(index) is not int or not 0 <= index < 100:
            raise NotFound("Image index is invalid")
        with closing(self._connect()) as db:
            self._question(db, question_id)
            result = db.execute(
                "SELECT i.bytes, i.mime_type FROM question_images qi JOIN images i ON i.key=qi.image_key "
                "WHERE qi.question_id=? ORDER BY qi.image_key LIMIT 1 OFFSET ?", (question_id, index),
            ).fetchone()
            if result is None or result[1] != "image/png" or not result[0].startswith(b"\x89PNG\r\n\x1a\n"):
                raise NotFound("Image unavailable")
            return bytes(result[0]), result[1]


def make_handler(store: ReviewStore):
    csrf_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "TOPIKLocalReview/1.0"

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

        def _json(self, status: int, content: dict):
            payload = json.dumps(content, ensure_ascii=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(payload))
            self.wfile.write(payload)

        def _origin(self) -> str:
            return f"http://127.0.0.1:{self.server.server_port}"

        def _host_valid(self) -> bool:
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

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
            parts = [unquote(piece) for piece in urlsplit(self.path).path.split("/") if piece]
            try:
                if not parts:
                    contents = HTML_PATH.read_bytes()
                    self._headers(200, "text/html; charset=utf-8", len(contents))
                    return self.wfile.write(contents)
                if parts == ["api", "questions"]:
                    return self._json(200, {**store.list_questions(), "csrf_token": csrf_token})
                if len(parts) == 3 and parts[:2] == ["api", "questions"]:
                    return self._json(200, store.get_question(parts[2]))
                if len(parts) == 3 and parts[0] == "media":
                    if parts[2] == "clip":
                        return self._file(store.clip_path(parts[1]))
                    return self._file(store.media_path(parts[1], parts[2]))
                if len(parts) == 4 and parts[0] == "media" and parts[2] == "image" and parts[3].isdigit():
                    image, mime = store.get_image(parts[1], int(parts[3]))
                    self._headers(200, mime, len(image))
                    return self.wfile.write(image)
                raise NotFound("Unknown page")
            except ReviewError as exc:
                return self._error(exc)
            except (OSError, sqlite3.Error):
                return self._json(500, {"error": "Local file or database unavailable"})

        def do_POST(self):
            if not self._host_valid() or self.headers.get("Origin") != self._origin():
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
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_POST_BYTES:
                    raise ReviewError("Review request too large or empty")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if parts[3] == "review":
                    result = store.save_review(parts[2], payload)
                elif parts[3] == "audio-segment":
                    result = store.save_audio_segment(parts[2], payload)
                else:
                    if payload != {}:
                        raise ReviewError("Audio export request must be an empty JSON object")
                    result = store.export_audio_clip(parts[2])
                return self._json(200, result)
            except ReviewError as exc:
                return self._error(exc)
            except (ValueError, UnicodeError) as exc:
                return self._json(400, {"error": str(exc)})
            except sqlite3.Error:
                return self._json(500, {"error": "Local database unavailable"})

        def log_message(self, fmt, *args):
            # Avoid printing source text, request bodies and local paths.
            return

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=None,
                        help="Local-only port (default: try 8765, then choose a free port; 0: OS-selected)")
    args = parser.parse_args()
    if args.port is not None and not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    store = ReviewStore()
    handler = make_handler(store)
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
        print(f"TOPIK 35 I review: http://127.0.0.1:{server.server_port}/")
        print("Press Ctrl+C to stop. Source PDFs and audio are read-only; only the local SQLite DB is edited.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Stopped.")


if __name__ == "__main__":
    main()
