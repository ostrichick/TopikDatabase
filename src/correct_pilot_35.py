"""Correct 35th TOPIK I extracted punctuation and contaminated image crops.

Run once: py -3 src/correct_pilot_35.py
Backs up the existing local SQLite database, preserves source PDFs/HTML/MP3,
never overwrites a human-reviewed or human-edited question, and never approves
the mechanically corrected material. Re-running after success is a no-op.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import pilot_35
from src.extraction_rules import (RULE_VERSION, clean_question_image,
                                  derived_image_key, normalize_punctuation_spacing)
from src.transcript_35 import extract_transcripts


def _source_snapshot(db: sqlite3.Connection, preview: Path) -> None:
    recorded = dict(db.execute("SELECT relative_path,sha256 FROM source_files"))
    expected_sources = pilot_35.load_source_entries()
    expected = {entry["path"]: entry["sha256"] for entry in expected_sources.values()}
    expected[preview.relative_to(ROOT).as_posix()] = pilot_35.digest(preview)
    if recorded != expected:
        raise RuntimeError("Source manifest and stored file hashes no longer match")
    for relative, sha256 in recorded.items():
        path = (ROOT / relative).resolve()
        try:
            path.relative_to(pilot_35.SESSION_DIR.resolve())
        except ValueError as exc:
            raise RuntimeError("A stored source path escapes the 35th corpus") from exc
        if not path.is_file() or pilot_35.digest(path) != sha256:
            raise RuntimeError("An original file was modified; corrections are blocked")


def _baseline(db: sqlite3.Connection, questions: list, groups: list,
              images: dict, transcripts: dict) -> None:
    """Refuse to overwrite any edits, even pending ones without a review record."""
    pilot_35.check_quality(db)
    if db.execute("SELECT count(*) FROM review_records WHERE subject_type='question'").fetchone()[0]:
        raise RuntimeError("Existing question review history must be reconciled before correction")
    if db.execute("SELECT count(*) FROM questions WHERE review_status<>'needs_manual_review'").fetchone()[0]:
        raise RuntimeError("Reviewed questions must not be changed automatically")
    if db.execute("SELECT count(*) FROM transcripts WHERE review_status<>'needs_manual_review'").fetchone()[0]:
        raise RuntimeError("Reviewed transcripts must not be changed automatically")
    for group in groups:
        row = db.execute("SELECT instruction,passage_text FROM question_groups WHERE id=?",
                         (group["group_id"],)).fetchone()
        if row is None or tuple(row) != (group["instruction"], group["passage_text"]):
            raise RuntimeError(f"Group edited after import: {group['group_id']}")
    for question in questions:
        qid = question["id"]
        row = db.execute("SELECT stem,raw_question_text FROM questions WHERE id=?", (qid,)).fetchone()
        if row is None or tuple(row) != (question["stem"], question["raw_question_text"]):
            raise RuntimeError(f"Question edited after import: {qid}")
        choices = list(db.execute("SELECT number,text FROM choices WHERE question_id=? ORDER BY number", (qid,)))
        if [(number, text) for number, text in choices] != [
                (i, question["options"][str(i)]) for i in range(1, 5)]:
            raise RuntimeError(f"Choices edited after import: {qid}")
        if question["section"] == "listening":
            record = db.execute("SELECT dialogue_text FROM transcripts WHERE question_id=?", (qid,)).fetchone()
            if record is None or record[0] != transcripts[question["number"]]["text"]:
                raise RuntimeError(f"Transcript edited after import: {qid}")
        actual_keys = [r[0] for r in db.execute(
            "SELECT image_key FROM question_images WHERE question_id=? ORDER BY image_key", (qid,))]
        if actual_keys != sorted(question["image_paths"]):
            raise RuntimeError(f"Image links edited after import: {qid}")
    for key, uri in images.items():
        original = base64.b64decode(uri.partition(",")[2], validate=True)
        row = db.execute("SELECT bytes,sha256 FROM images WHERE key=?", (key,)).fetchone()
        if row is None or row[0] != original or row[1] != hashlib.sha256(original).hexdigest():
            raise RuntimeError(f"Original image edited after import: {key}")


def correct_db(db_path: Path = pilot_35.DB_PATH, backup_path: Path | None = None) -> dict:
    """Apply exactly once, returning changed counts and the preserved backup."""
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    preview_paths = list(pilot_35.SESSION_DIR.glob("*.html"))
    if len(preview_paths) != 1:
        raise RuntimeError("Expected one original 35th HTML extraction")
    preview = preview_paths[0]
    questions, groups, images = pilot_35.load_preview(preview)
    # This historical correction compares against the exact pre-layout-spacing
    # import baseline. A later, separately versioned migration handles PDF
    # visual word gaps without overwriting human-reviewed dialogue.
    transcripts = extract_transcripts(
        pilot_35.SESSION_DIR / "35th-TOPIK-I-Listening-Transcript.pdf",
        restore_visual_spacing=False,
    )
    if (len(questions), len(groups), len(images), len(transcripts)) != (70, 26, 6, 30):
        raise RuntimeError("Unexpected baseline data")

    with closing(sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True, timeout=5)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        metadata = dict(db.execute("SELECT key,value FROM import_metadata"))
        if metadata.get("preview_sha256") != pilot_35.digest(preview):
            raise RuntimeError("Existing DB does not match the immutable preview")
        _source_snapshot(db, preview)
        if metadata.get("extraction_correction_version") == RULE_VERSION:
            pilot_35.check_quality(db)
            return {"already_corrected": True, "rule_version": RULE_VERSION, "backup": None}
        if metadata.get("extraction_correction_version"):
            raise RuntimeError("Unknown correction version; manual reconciliation required")
        _baseline(db, questions, groups, images, transcripts)

        replacements = {}
        changed_texts = 0
        for group in groups:
            for name in ("instruction", "passage_text"):
                changed_texts += len(normalize_punctuation_spacing(group[name])) - len(group[name])
        for question in questions:
            changed_texts += len(normalize_punctuation_spacing(question["stem"])) - len(question["stem"])
            for value in question["options"].values():
                changed_texts += len(normalize_punctuation_spacing(value)) - len(value)
        for transcript in transcripts.values():
            changed_texts += len(normalize_punctuation_spacing(transcript["text"])) - len(transcript["text"])
        for key, uri in images.items():
            original = base64.b64decode(uri.partition(",")[2], validate=True)
            cleaned = clean_question_image(key, original)
            if cleaned != original:
                replacements[key] = (derived_image_key(key), cleaned)
        if changed_texts != 128 or len(replacements) != 5:
            raise RuntimeError(f"Unexpected correction extent: text={changed_texts} images={len(replacements)}")

        if backup_path is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = db_path.with_name(
                f"{db_path.stem}.before-extraction-correction-{stamp}-{secrets.token_hex(3)}.sqlite")
        backup_path = Path(backup_path).resolve()
        if backup_path == db_path or backup_path.exists() or not backup_path.parent.is_dir():
            raise RuntimeError("Backup destination must be a new file in an existing directory")
        # A transactionally consistent backup, never a raw copy of a live WAL DB.
        # O_EXCL prevents overwriting a user-created backup.
        import os
        descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(backup_path)) as destination:
            db.backup(destination)
        with closing(sqlite3.connect(backup_path.as_uri() + "?mode=ro", uri=True)) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup failed SQLite integrity check")

        db.execute("BEGIN IMMEDIATE")
        try:
            _baseline(db, questions, groups, images, transcripts)
            affected: dict[str, dict] = {}

            def record(qid: str, field: str, before, after):
                affected.setdefault(qid, {})[field] = {"before": before, "after": after}

            for group in groups:
                gid = group["group_id"]
                for field in ("instruction", "passage_text"):
                    before = group[field]
                    after = normalize_punctuation_spacing(before)
                    if before != after:
                        db.execute(f"UPDATE question_groups SET {field}=? WHERE id=?", (after, gid))
                        for qid, in db.execute("SELECT id FROM questions WHERE group_id=?", (gid,)):
                            record(qid, "group_" + field, before, after)
            for question in questions:
                qid = question["id"]
                before = question["stem"]
                after = normalize_punctuation_spacing(before)
                if before != after:
                    db.execute("UPDATE questions SET stem=? WHERE id=?", (after, qid))
                    record(qid, "stem", before, after)
                for number, before in question["options"].items():
                    after = normalize_punctuation_spacing(before)
                    if before != after:
                        db.execute("UPDATE choices SET text=? WHERE question_id=? AND number=?",
                                   (after, qid, int(number)))
                        record(qid, "choice_" + number, before, after)
                if question["section"] == "listening":
                    before = transcripts[question["number"]]["text"]
                    after = normalize_punctuation_spacing(before)
                    if before != after:
                        db.execute("UPDATE transcripts SET dialogue_text=? WHERE question_id=?", (after, qid))
                        record(qid, "transcript", before, after)
                for old_key in question["image_paths"]:
                    if old_key in replacements:
                        new_key, blob = replacements[old_key]
                        record(qid, "image", {"key": old_key, "sha256": hashlib.sha256(
                            base64.b64decode(images[old_key].partition(",")[2], validate=True)).hexdigest()},
                            {"key": new_key, "sha256": hashlib.sha256(blob).hexdigest()})

            for old_key, (new_key, blob) in replacements.items():
                source_id, = db.execute("SELECT source_file_id FROM images WHERE key=?", (old_key,)).fetchone()
                db.execute("INSERT INTO images(key,mime_type,sha256,bytes,source_file_id) VALUES(?,?,?,?,?)",
                           (new_key, "image/png", hashlib.sha256(blob).hexdigest(), blob, source_id))
                db.execute("UPDATE question_images SET image_key=? WHERE image_key=?", (new_key, old_key))

            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for qid, changed in affected.items():
                db.execute("INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                           "VALUES(?,?,?,?,?,?,?)", (
                               "question", qid, "needs_manual_review", "extraction_rule_v1",
                               "extraction_correction_35", json.dumps({
                                   "note": "Automated punctuation spacing and/or pure-image correction; human review still required",
                                   "changes": changed,
                               }, ensure_ascii=False), now,
                           ))
            db.execute("INSERT INTO import_metadata(key,value) VALUES(?,?)",
                       ("extraction_correction_version", RULE_VERSION))
            report = pilot_35.check_quality(db)
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Corrected database failed SQLite integrity check")
            db.commit()
        except BaseException:
            db.rollback()
            raise

    return {"already_corrected": False, "rule_version": RULE_VERSION,
            "backup": str(backup_path), "inserted_punctuation_spaces": changed_texts,
            "corrected_image_assets": len(replacements),
            "affected_questions": len(affected), "questions": report["questions"],
            "review_status": report["review_status"]}


if __name__ == "__main__":
    try:
        print(json.dumps(correct_db(), ensure_ascii=False, indent=2))
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"Correction blocked: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
