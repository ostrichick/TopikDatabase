"""Back up the 35th TOPIK I pilot and register UNVERIFIED audio-cut proposals.

Run from project root with the review server stopped:
    py -3 src/prepare_audio_35.py

All proposed boundaries require a person's listening confirmation. Original
PDF/MP3, text reviews, answers and images are never modified by this script.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import audio_35, pilot_35


VERSION = "silence31-unverified-v1"
REPORT_PATH = ROOT / "topik-past-papers" / "derived" / "035-I-B-audio-candidates.json"


def _table_sql() -> str:
    """Reuse the canonical schema definition rather than maintaining two copies."""
    source = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    match = re.search(r"(?ms)^CREATE TABLE IF NOT EXISTS audio_segments\s*\(.*?^\);", source)
    if match is None:
        raise RuntimeError("Audio table definition is missing from db/schema.sql")
    return match.group(0)


def _atomic_report(path: Path, data: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".audio-report-", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def prepare_audio_db(db_path: Path = pilot_35.DB_PATH, report_path: Path = REPORT_PATH) -> dict:
    """Register 27 candidate intervals linked to 30 questions, never approve."""
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    analysis = audio_35.analyze_audio()
    candidates = analysis["candidates"]
    if (not analysis["thresholds_stable"] or len(candidates) != 27 or
            [n for row in candidates for n in row["questions"]] != list(range(1, 31)) or
            any(row["status"] != "candidate_unverified" for row in candidates)):
        raise RuntimeError("Recording does not support the expected 30 provisional question assignments")

    with closing(sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True, timeout=5)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Existing pilot SQLite database did not pass integrity_check")
        if db.execute("SELECT session,level,booklet FROM exams").fetchall() != [(35, "I", "B")]:
            raise RuntimeError("This migration only supports 35th TOPIK I booklet B")
        source = db.execute(
            "SELECT s.sha256,s.byte_size FROM audio_assets a JOIN source_files s "
            "ON s.id=a.source_file_id WHERE a.id='035-I-B-audio'",
        ).fetchone()
        if source is None or source[0] != analysis["source_sha256"] or source[1] != audio_35.DEFAULT_SOURCE.stat().st_size:
            raise RuntimeError("MP3 differs from the immutable source manifest")
        pilot_35.check_quality(db)
        prior_duration = db.execute("SELECT duration_seconds FROM audio_assets WHERE id='035-I-B-audio'").fetchone()[0]
        if prior_duration is not None and abs(prior_duration * 1000 - analysis["duration_ms"]) > 2:
            raise RuntimeError("Audio duration metadata conflicts with the source MP3")
        table_present = bool(db.execute("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='audio_segments' "
                                        "AND type='table')").fetchone()[0])
        previous = dict(db.execute("SELECT key,value FROM import_metadata"))
        if previous.get("audio_detection_version") not in (None, VERSION):
            raise RuntimeError("Unrecognized audio detection version; manual migration required")
        if previous.get("audio_detection_source_sha256") not in (None, analysis["source_sha256"]):
            raise RuntimeError("Audio detection belongs to another source file")
        if table_present:
            existing = dict(db.execute("SELECT question_id,status FROM audio_segments"))
            if len(existing) == 30 and previous.get("audio_detection_version") == VERSION:
                return {"already_prepared": True, "candidate_question_count": sum(
                    value == "candidate" for value in existing.values()),
                    "verified_question_count": sum(value == "verified" for value in existing.values()),
                    "candidate_clips": len(candidates), "backup": None, "report": str(report_path)}

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = db_path.with_name(f"{db_path.stem}.before-audio-{stamp}-{secrets.token_hex(3)}.sqlite")
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(backup)) as destination:
            db.backup(destination)
        with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("New pre-audio SQLite backup failed integrity_check")

        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(_table_sql())
            db.execute("UPDATE audio_assets SET duration_seconds=?,timing_status='candidate_unverified' "
                       "WHERE id='035-I-B-audio'",
                       (analysis["duration_ms"] / 1000,))
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            inserted = 0
            preserved = 0
            for proposal in candidates:
                for number in proposal["questions"]:
                    qid = f"035-I-L-{number:03d}"
                    stored = db.execute("SELECT source_sha256 FROM audio_segments WHERE question_id=?",
                                        (qid,)).fetchone()
                    if stored:
                        if stored[0] != analysis["source_sha256"]:
                            raise RuntimeError(f"Existing segment belongs to another MP3: {qid}")
                        preserved += 1
                        continue
                    db.execute(
                        "INSERT INTO audio_segments(question_id,audio_asset_id,start_ms,end_ms,status,version,"
                        "source_sha256,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (qid, "035-I-B-audio", proposal["start_ms"], proposal["end_ms"],
                         "candidate", 1, analysis["source_sha256"], now),
                    )
                    db.execute(
                        "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        ("audio_segment", qid, "candidate", "silence_detector", "automatic_audio_candidate_35",
                         json.dumps({"note": "침묵 기반 미확정 구간. 듣고 문항과 경계를 확인하세요.",
                                     "start_ms": proposal["start_ms"], "end_ms": proposal["end_ms"],
                                     "shared_questions": proposal["questions"],
                                     "source_sha256": analysis["source_sha256"],
                                     "method": proposal["method"]}, ensure_ascii=False), now),
                    )
                    inserted += 1
            db.execute("INSERT OR REPLACE INTO import_metadata(key,value) VALUES(?,?)",
                       ("audio_detection_version", VERSION))
            db.execute("INSERT OR REPLACE INTO import_metadata(key,value) VALUES(?,?)",
                       ("audio_detection_source_sha256", analysis["source_sha256"]))
            if db.execute("PRAGMA foreign_key_check").fetchall() or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Audio proposal import failed database integrity checks")
            pilot_35.check_quality(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise

    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_report(report_path, analysis)
    return {"already_prepared": False, "candidate_question_count": inserted,
            "preserved_segments": preserved, "candidate_clips": len(candidates),
            "backup": str(backup), "report": str(report_path),
            "source_duration_ms": analysis["duration_ms"], "all_boundaries_unverified": True}


if __name__ == "__main__":
    try:
        print(json.dumps(prepare_audio_db(), ensure_ascii=False, indent=2))
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
        print(f"Audio setup blocked: {error}", file=sys.stderr)
        raise SystemExit(1) from error
