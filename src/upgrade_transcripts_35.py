"""Restore word spaces in 35th TOPIK I listening dialogue without losing reviews.

Run with the review server stopped: py -3 src/upgrade_transcripts_35.py
Original PDFs, audio, answers, choices, approved questions, and past review
records are immutable. This creates a consistent SQLite backup before updating
only unchanged, pending dialogue and logs every correction.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import pilot_35
from src.correct_pilot_35 import _source_snapshot
from src.extraction_rules import RULE_VERSION, normalize_punctuation_spacing
from src.transcript_35 import extract_transcripts


VERSION = "pypdf-word-spacing-v1"
HISTORY_SCOPE = "transcript_word_spacing_35"
SOURCE = pilot_35.SESSION_DIR / "35th-TOPIK-I-Listening-Transcript.pdf"


def _snapshot(db: sqlite3.Connection) -> tuple:
    """Include all 30 transcripts, statuses and review history in race checks."""
    rows = tuple(db.execute(
        "SELECT q.id,q.exam_number,q.review_status,t.dialogue_text,t.review_status,"
        "t.warnings_json,t.source_pdf_page FROM questions q JOIN transcripts t "
        "ON t.question_id=q.id ORDER BY q.exam_number"
    ))
    history = tuple(db.execute(
        "SELECT id,subject_type,subject_id,status,scope,evidence,reviewed_at "
        "FROM review_records ORDER BY id"
    ))
    return rows, history, tuple(db.execute("SELECT key,value FROM import_metadata ORDER BY key"))


def _sources_and_schema(db: sqlite3.Connection) -> dict:
    metadata = dict(db.execute("SELECT key,value FROM import_metadata"))
    if metadata.get("schema_version") != "pilot-1" or metadata.get("extraction_correction_version") != RULE_VERSION:
        raise RuntimeError("Expected the corrected 35th TOPIK I pilot database")
    previews = list(pilot_35.SESSION_DIR.glob("*.html"))
    if len(previews) != 1 or metadata.get("preview_sha256") != pilot_35.digest(previews[0]):
        raise RuntimeError("Original preview is missing or changed")
    _source_snapshot(db, previews[0])
    pilot_35.check_quality(db)
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("Original SQLite database failed integrity check")
    return metadata


def _candidates(db: sqlite3.Connection, old: dict, new: dict) -> tuple[list, list, list]:
    updates, reviewed, edited = [], [], []
    for number in range(1, 31):
        qid = f"035-I-L-{number:03d}"
        row = db.execute(
            "SELECT q.review_status,t.dialogue_text,t.review_status,t.source_pdf_page "
            "FROM questions q JOIN transcripts t ON t.question_id=q.id WHERE q.id=?", (qid,)
        ).fetchone()
        if row is None or row[3] != old[number]["pdf_page"] or row[3] != new[number]["pdf_page"]:
            raise RuntimeError(f"Transcript missing or page mismatch: {qid}")
        baseline = normalize_punctuation_spacing(old[number]["text"])
        candidate = new[number]["text"]
        if "".join(baseline.split()) != "".join(candidate.split()):
            raise RuntimeError(f"PDF extraction changed non-whitespace characters: {qid}")
        is_human_reviewed = db.execute(
            "SELECT EXISTS(SELECT 1 FROM review_records WHERE subject_type='question' "
            "AND subject_id=? AND scope='manual_question_review')", (qid,)
        ).fetchone()[0]
        if row[0] != "needs_manual_review" or row[2] != "needs_manual_review" or is_human_reviewed:
            reviewed.append(number)
            continue
        if row[1] != baseline:
            # Even an unlogged draft edit belongs to the user, not an importer.
            edited.append(number)
            continue
        if row[1] != candidate:
            updates.append((number, qid, row[1], candidate))
    return updates, reviewed, edited


def upgrade_db(db_path: Path = pilot_35.DB_PATH, backup_path: Path | None = None) -> dict:
    """Versioned, transactional correction on a single existing SQLite DB."""
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    old = extract_transcripts(SOURCE, restore_visual_spacing=False)
    new = extract_transcripts(SOURCE, restore_visual_spacing=True)
    if set(old) != set(range(1, 31)) or set(new) != set(old):
        raise RuntimeError("Both extraction engines must yield the same 30 questions")

    with closing(sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True, timeout=5)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        metadata = _sources_and_schema(db)
        if metadata.get("transcript_extraction_version") == VERSION:
            return {"already_corrected": True, "version": VERSION, "backup": None,
                    "updated_questions": [], "skipped_reviewed": [], "skipped_edited": []}
        if metadata.get("transcript_extraction_version"):
            raise RuntimeError("Unknown transcript version; refuse to overwrite it")
        baseline = _snapshot(db)
        updates, reviewed, edited = _candidates(db, old, new)

        if backup_path is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = db_path.with_name(
                f"{db_path.stem}.before-word-spacing-{stamp}-{secrets.token_hex(3)}.sqlite")
        backup_path = Path(backup_path).resolve()
        if backup_path == db_path or backup_path.exists() or not backup_path.parent.is_dir():
            raise RuntimeError("Backup must be a new file in an existing directory")

        # SQLite's backup API is consistent even if the source has a WAL file.
        descriptor = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(backup_path)) as dest:
            db.backup(dest)
        with closing(sqlite3.connect(backup_path.as_uri() + "?mode=ro", uri=True)) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Word-spacing backup failed integrity check")

        db.execute("BEGIN IMMEDIATE")
        try:
            if _snapshot(db) != baseline:
                raise RuntimeError("A review was saved during the backup; no corrections applied")
            _sources_and_schema(db)
            if _candidates(db, old, new) != (updates, reviewed, edited):
                raise RuntimeError("Transcript changed during backup; no corrections applied")

            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for number, qid, before, after in updates:
                existing_warnings, = db.execute(
                    "SELECT warnings_json FROM transcripts WHERE question_id=?", (qid,)
                ).fetchone()
                warnings = json.loads(existing_warnings)
                if not isinstance(warnings, list):
                    raise RuntimeError(f"Invalid transcript warnings: {qid}")
                warnings.append("Word spaces restored from original PDF using pypdf; confirm manually")
                db.execute(
                    "UPDATE transcripts SET dialogue_text=?,warnings_json=? WHERE question_id=? "
                    "AND dialogue_text=? AND review_status='needs_manual_review'",
                    (after, json.dumps(warnings, ensure_ascii=False), qid, before),
                )
                if db.execute("SELECT changes()").fetchone()[0] != 1:
                    raise RuntimeError(f"Concurrent edit prevented correction: {qid}")
                db.execute(
                    "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    ("question", qid, "needs_manual_review", "source_pdf_extractor", HISTORY_SCOPE,
                     json.dumps({"note": "원본 PDF에서 단어 띄어쓰기를 복원했습니다. 대본은 다시 검토해 주세요.",
                                 "before": before, "after": after,
                                 "source_pdf_sha256": pilot_35.digest(SOURCE),
                                 "source_pdf_page": old[number]["pdf_page"]}, ensure_ascii=False), now),
                )
            db.execute("INSERT INTO import_metadata(key,value) VALUES(?,?)", ("transcript_extraction_version", VERSION))
            pilot_35.check_quality(db)
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Word-spacing migration failed SQLite integrity check")
            db.commit()
        except BaseException:
            db.rollback()
            raise

    return {"already_corrected": False, "version": VERSION,
            "backup": str(backup_path), "updated_questions": [item[0] for item in updates],
            "skipped_reviewed": reviewed, "skipped_edited": edited,
            "inserted_spaces": sum(len(item[3]) - len(item[2]) for item in updates)}


if __name__ == "__main__":
    try:
        print(json.dumps(upgrade_db(), ensure_ascii=False, indent=2))
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
        print(f"Transcript spacing upgrade blocked: {error}", file=sys.stderr)
        raise SystemExit(1) from error
