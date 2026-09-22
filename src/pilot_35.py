"""Import the existing 35th TOPIK I preview into a provenance-aware local SQLite DB.

This pilot uses the pre-existing, unapproved HTML extraction as input. It checks
its answers independently against the PDF answer table, links the complete
transcript, and deliberately DOES NOT mark question content human-verified.
No PDF, audio, HTML, or source manifest is modified.

From the repository root: py -3 src/pilot_35.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path



ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "topik-past-papers"
SESSION_DIR = CORPUS / "35th"
SCHEMA = ROOT / "db" / "schema.sql"
OUTPUT_DIR = CORPUS / "derived"
DB_PATH = OUTPUT_DIR / "035-I-B.sqlite"
REPORT_PATH = OUTPUT_DIR / "035-I-B-report.json"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CORPUS / ".verification_deps"))

from src.extraction_rules import (RULE_VERSION, clean_question_image,
                                  derived_image_key, normalize_punctuation_spacing)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_preview(path: Path) -> tuple[list, list, dict]:
    # Read HTML as TEXT ONLY. Never execute its third-party script.
    html = path.read_text(encoding="utf-8")
    values = []
    for variable in ("QUESTIONS", "GROUPS", "IMAGES"):
        found = re.search(rf"(?m)^const {variable}=(.*);\s*$", html)
        if found is None:
            raise ValueError(f"Preview has no JSON variable: {variable}")
        values.append(json.loads(found.group(1)))
    return tuple(values)


def load_source_entries() -> dict[tuple[str, str], dict]:
    manifest = json.loads((CORPUS / "manifest_early.json").read_text(encoding="utf-8"))
    entries = [entry for entry in manifest["entries"] if entry["session"] == 35 and entry["level"] == "I"]
    indexed = {(entry["asset"], entry["section"]): entry for entry in entries}
    required = (("test_paper", "combined"), ("answer_key", "combined"),
                ("listening_audio", "combined"), ("listening_transcript", "combined"))
    if len(entries) != 4 or any(key not in indexed for key in required):
        raise ValueError("Expected exactly four source-linked TOPIK I artifacts")
    return indexed


def register_source(conn: sqlite3.Connection, path: Path, kind: str,
                    entry: dict | None = None) -> int:
    relative = path.relative_to(ROOT).as_posix()
    actual_hash = digest(path)
    if entry:
        if entry["path"].replace("\\", "/") != relative or entry["sha256"] != actual_hash:
            raise ValueError(f"Manifest path/hash mismatch for {relative}")
        if entry["size_bytes"] != path.stat().st_size:
            raise ValueError(f"Manifest size mismatch for {relative}")
    cursor = conn.execute(
        "INSERT INTO source_files(relative_path,kind,sha256,byte_size,source_url,source_page) "
        "VALUES(?,?,?,?,?,?)",
        (relative, kind, actual_hash, path.stat().st_size,
         entry.get("url") if entry else None, entry.get("source_page") if entry else None),
    )
    return cursor.lastrowid


def pdf_answer_table(answer_path: Path) -> dict[str, dict[int, tuple[int, int, int]]]:
    """Parse printed (question, choice, points) triples in the original two pages.

    Page 1 listening is numbered 1..30; page 2 reading is numbered 1..40.
    Page 2's local number must be offset by 30 to match the booklet's 31..70.
    Parsing fails closed on any missing/duplicate/invalid value.
    """
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("Install pymupdf in topik-past-papers/.verification_deps") from exc

    result: dict[str, dict[int, tuple[int, int, int]]] = {}
    with pymupdf.open(str(answer_path)) as pdf:
        if len(pdf) != 2:
            raise ValueError("TOPIK I answer key should have exactly two pages")
        for page_number, (section, expected) in enumerate((("listening", 30), ("reading", 40)), 1):
            content = pdf[page_number - 1].get_text()
            if section == "listening" and "듣기" not in content:
                raise ValueError("Listening answer page mislabeled")
            if section == "reading" and "읽기" not in content:
                raise ValueError("Reading answer page mislabeled")
            if "배점" not in content or "답지" not in content:
                raise ValueError("Could not locate answer table boundaries")
            cells = content.split("배점")[-1].split("답지", 1)[0].split()
            if len(cells) != 3 * expected:
                raise ValueError(f"{section}: expected {3 * expected} table cells; got {len(cells)}")
            answers = {}
            for pos in range(0, len(cells), 3):
                number, choice, points = cells[pos:pos + 3]
                number, points = int(number), int(points)
                choice = str("①②③④".index(choice) + 1) if choice in "①②③④" else choice
                if not (1 <= number <= expected and choice in ("1", "2", "3", "4") and points in (2, 3, 4)):
                    raise ValueError(f"Unexpected answer row: {number}/{choice}/{points}")
                if number in answers:
                    raise ValueError(f"Duplicate answer row {section} {number}")
                answers[number] = (int(choice), points, page_number)
            if set(answers) != set(range(1, expected + 1)):
                raise ValueError(f"Nonconsecutive answer numbering in {section}")
            if sum(value[1] for value in answers.values()) != 100:
                raise ValueError(f"{section}: point total is not 100")
            result[section] = answers
    return result


def _report(conn: sqlite3.Connection, *, unchanged: bool = False) -> dict:
    scalar = lambda sql: conn.execute(sql).fetchone()[0]
    try:
        database_display_path = DB_PATH.relative_to(ROOT).as_posix()
    except ValueError:
        database_display_path = str(DB_PATH)
    counts = dict(conn.execute("SELECT name, count(*) FROM sections JOIN questions "
                               "ON questions.section_id=sections.id GROUP BY name"))
    review = dict(conn.execute("SELECT review_status, count(*) FROM questions GROUP BY review_status"))
    has_segments = bool(scalar("SELECT EXISTS(SELECT 1 FROM sqlite_master "
                               "WHERE type='table' AND name='audio_segments')"))
    audio_status = (dict(conn.execute("SELECT status, count(*) FROM audio_segments GROUP BY status"))
                    if has_segments else {})
    timed_segments = scalar("SELECT count(*) FROM audio_segments") if has_segments else 0
    exported_segments = (scalar("SELECT count(*) FROM audio_segments WHERE status='verified' "
                                "AND clip_relative_path IS NOT NULL") if has_segments else 0)
    image_q = [item[0] for item in conn.execute(
        "SELECT exam_number FROM questions WHERE requires_image=1 ORDER BY exam_number")]
    return {
        "scope": "35th TOPIK I, B booklet ONLY",
        "database": database_display_path,
        "source_pdf_and_preview_hash_verified": True,
        "source_files": scalar("SELECT count(*) FROM source_files"),
        "sections": counts,
        "questions": scalar("SELECT count(*) FROM questions"),
        "choices": scalar("SELECT count(*) FROM choices"),
        "answers": scalar("SELECT count(*) FROM answers"),
        "answers_matching_pdf": scalar("SELECT count(*) FROM answers WHERE preview_and_pdf_agree=1"),
        "transcripts": scalar("SELECT count(*) FROM transcripts"),
        "image_blobs": scalar("SELECT count(*) FROM images"),
        "active_image_blobs": scalar("SELECT count(DISTINCT image_key) FROM question_images"),
        "image_question_links": scalar("SELECT count(*) FROM question_images"),
        "image_required_questions": image_q,
        "audio_segments_with_timestamps": timed_segments,
        "audio_segment_status": audio_status,
        "audio_segments_with_exported_clip": exported_segments,
        "review_status": review,
        "manual_review_required": (review.get("needs_manual_review", 0) > 0 or
                                   review.get("rejected", 0) > 0 or
                                   audio_status.get("verified", 0) != 30),
        "extraction_correction_version": dict(conn.execute("SELECT key,value FROM import_metadata")).get(
            "extraction_correction_version", "not_applied"),
        "limitations": [
            "The original HTML preview was not human-approved; use review_status for current per-question progress.",
            "PDF answer values and points are mechanically cross-checked; their semantic correctness is not independently certified.",
            "Transcript text comes from PDF extraction and must be compared visually with the original.",
            "Image crops were embedded in the original preview and need manual layout checks.",
            "Audio intervals are unverified until a person checks the recording; candidate timestamps are not approval.",
            "Source material and derived database are local and not licensed for redistribution by this import.",
        ],
        "reused_existing_database": unchanged,
    }


def check_quality(conn: sqlite3.Connection, *, fresh_import: bool = False) -> dict:
    report = _report(conn)
    expected_blobs = (11 if report["extraction_correction_version"] == RULE_VERSION else 6)
    if (report["questions"] != 70 or report["sections"] != {"listening": 30, "reading": 40} or
            report["choices"] != 280 or report["answers_matching_pdf"] != 70 or
            report["transcripts"] != 30 or report["image_blobs"] != expected_blobs or
            report["active_image_blobs"] != 6 or
            report["image_question_links"] != 7 or
            sum(report["review_status"].values()) != 70 or
            set(report["review_status"]) - {"needs_manual_review", "verified", "rejected"}):
        raise ValueError(f"Pilot quality gate failed: {report}")
    if fresh_import and report["review_status"] != {"needs_manual_review": 70}:
        raise ValueError("Fresh imports must remain pending human review")
    if fresh_import and report["extraction_correction_version"] != RULE_VERSION:
        raise ValueError("Fresh imports must use approved extraction correction rules")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("Pilot contains foreign key violations")
    if conn.execute("SELECT count(*) FROM questions q JOIN answers a ON a.question_id=q.id "
                    "WHERE q.points NOT IN (2,3,4) OR a.preview_and_pdf_agree<>1").fetchone()[0]:
        raise ValueError("Invalid answer or point values")
    if conn.execute(
            "SELECT count(*) FROM questions q WHERE q.review_status IN ('verified','rejected') "
            "AND NOT EXISTS (SELECT 1 FROM review_records r WHERE r.subject_type='question' "
            "AND r.subject_id=q.id AND r.scope='manual_question_review' "
            "AND r.status=q.review_status)"
    ).fetchone()[0]:
        raise ValueError("Question marked reviewed without a matching review record")
    if conn.execute(
            "SELECT count(*) FROM transcripts t JOIN questions q ON q.id=t.question_id "
            "WHERE t.review_status<>q.review_status"
    ).fetchone()[0]:
        raise ValueError("Question and transcript review status disagree")
    return report


def atomic_json(path: Path, data: dict) -> None:
    fd, filename = tempfile.mkstemp(prefix=".report-", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(filename, path)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def main() -> dict:
    preview_paths = list(SESSION_DIR.glob("*.html"))
    if len(preview_paths) != 1:
        raise ValueError("Expected exactly one 35th TOPIK I preview")
    preview = preview_paths[0]
    questions, groups, images = load_preview(preview)
    entries = load_source_entries()
    paths = {kind: ROOT / entry["path"] for (kind, section), entry in entries.items()}
    for kind, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"Missing {kind}: {path}")
        if digest(path) != entries[(kind, "combined")]["sha256"]:
            raise ValueError(f"Modified {kind}: {path}")
    if len(questions) != 70 or len({q["id"] for q in questions}) != 70:
        raise ValueError("Preview must contain 70 distinct question IDs")

    answers = pdf_answer_table(paths["answer_key"])
    exam_numbers = Counter((q["section"], q["number"]) for q in questions)
    if set(exam_numbers) != ({("listening", n) for n in range(1, 31)} |
                            {("reading", n) for n in range(31, 71)}) or max(exam_numbers.values()) != 1:
        raise ValueError("Preview questions do not cover listening 1-30 and reading 31-70 exactly")
    for q in questions:
        section = q["section"]
        local_answer_number = q["number"] - (30 if section == "reading" else 0)
        correct, points, _ = answers[section][local_answer_number]
        if q["answer"] != correct or q["points"] != points:
            raise ValueError(f"Answer/point discrepancy in question {q['id']}")
        if q["session"] != 35 or q["level"] != "TOPIK_I" or q["booklet_type"] != "B":
            raise ValueError(f"Question belongs to another exam: {q['id']}")
        if q["source_file"] != paths["test_paper"].name or q["source_sha256"] != entries[("test_paper", "combined")]["sha256"]:
            raise ValueError(f"Question source mismatch: {q['id']}")
        if q["answer_key_source"] != paths["answer_key"].name or q["answer_key_sha256"] != entries[("answer_key", "combined")]["sha256"]:
            raise ValueError(f"Answer source mismatch: {q['id']}")
        if q["pdf_page"] != q["printed_page"] + 2:
            raise ValueError(f"Printed and PDF page mismatch: {q['id']}")
        if sorted(q["options"]) != ["1", "2", "3", "4"] or q["review_status"] != "needs_manual_review":
            raise ValueError(f"Unexpected choice/review structure: {q['id']}")
        if not (q["image_paths"] or any(q["options"].values())):
            raise ValueError(f"No textual or pictorial choices: {q['id']}")
        if q["requires_image"] != bool(q["image_paths"]):
            raise ValueError(f"Image flag/reference discrepancy: {q['id']}")
        if not all(key in images for key in q["image_paths"]):
            raise ValueError(f"Image blob missing: {q['id']}")

    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("Install pymupdf in topik-past-papers/.verification_deps") from exc
    with pymupdf.open(str(paths["test_paper"])) as paper:
        paper_pages = len(paper)
    if paper_pages != 27 or any(not 1 <= q["pdf_page"] <= paper_pages for q in questions):
        raise ValueError("Invalid PDF page references")

    # This module extracts source speech, not audio timestamps or verified speech recognition.
    from src.transcript_35 import extract_transcripts
    transcripts = extract_transcripts(paths["listening_transcript"])
    if set(transcripts) != set(range(1, 31)):
        raise ValueError("Transcript must cover every listening question 1-30")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    preview_hash = digest(preview)
    if DB_PATH.exists():
        connection = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
        try:
            previous = dict(connection.execute("SELECT key,value FROM import_metadata"))
            if previous.get("preview_sha256") != preview_hash or previous.get("schema_version") != "pilot-1":
                raise RuntimeError("An existing pilot database differs; it will not be overwritten")
            actual_sources = dict(connection.execute("SELECT relative_path,sha256 FROM source_files"))
            expected_paths = list(paths.values()) + [preview]
            if actual_sources != {p.relative_to(ROOT).as_posix(): digest(p) for p in expected_paths}:
                raise RuntimeError("An existing pilot DB source changed; manual reconciliation required")
            report = check_quality(connection)
            report["reused_existing_database"] = True
        finally:
            connection.close()
        atomic_json(REPORT_PATH, report)
        return report

    fd, temp_filename = tempfile.mkstemp(prefix=".035-I-", suffix=".sqlite.part", dir=OUTPUT_DIR)
    os.close(fd)
    try:
        connection = sqlite3.connect(temp_filename)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(SCHEMA.read_text(encoding="utf-8"))
            connection.execute("BEGIN")
            connection.execute("INSERT INTO exams VALUES(?,?,?,?)", ("035-I-B", 35, "I", "B"))
            for section, start, end, offset in (("listening", 1, 30, 0), ("reading", 31, 70, 30)):
                connection.execute("INSERT INTO sections VALUES(?,?,?,?,?,?)",
                                   (f"035-I-B-{section}", "035-I-B", section, start, end, offset))

            ids = {}
            for (kind, section), entry in entries.items():
                ids[kind] = register_source(connection, ROOT / entry["path"], kind, entry)
            ids["preview"] = register_source(connection, preview, "unapproved_html_preview")

            for group in groups:
                if group["section"] not in ("listening", "reading"):
                    raise ValueError(f"Unknown group section: {group['group_id']}")
                connection.execute("INSERT INTO question_groups VALUES(?,?,?,?,?,?,?,?)", (
                    group["group_id"], f"035-I-B-{group['section']}", group["start_number"],
                    group["end_number"], normalize_punctuation_spacing(group["instruction"]),
                    normalize_punctuation_spacing(group["passage_text"]),
                    group["points_each"], group["passage_image"],
                ))

            for key, uri in images.items():
                prefix, separator, encoded = uri.partition(",")
                if not separator or prefix != "data:image/png;base64":
                    raise ValueError(f"Unknown image MIME type: {key}")
                payload = base64.b64decode(encoded, validate=True)
                if not payload.startswith(b"\x89PNG\r\n\x1a\n") or len(payload) < 40:
                    raise ValueError(f"Invalid PNG image: {key}")
                connection.execute("INSERT INTO images VALUES(?,?,?,?,?)", (
                    key, "image/png", hashlib.sha256(payload).hexdigest(), payload, ids["preview"]
                ))
                cleaned = clean_question_image(key, payload)
                if cleaned != payload:
                    connection.execute("INSERT INTO images VALUES(?,?,?,?,?)", (
                        derived_image_key(key), "image/png", hashlib.sha256(cleaned).hexdigest(),
                        cleaned, ids["preview"],
                    ))

            for question in questions:
                section = question["section"]
                local_number = question["number"] - (30 if section == "reading" else 0)
                answer_choice, points, answer_page = answers[section][local_number]
                group_id = question["group_id"]
                connection.execute(
                    "INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (question["id"], f"035-I-B-{section}", group_id, ids["test_paper"],
                     question["number"], local_number, question["pdf_page"],
                     question["printed_page"], points, normalize_punctuation_spacing(question["stem"]),
                     question["raw_question_text"], question["passage_id"],
                     int(question["requires_image"]), "needs_manual_review", "unapproved_html_preview",
                     json.dumps(question["missing_source_parts"], ensure_ascii=False),
                     ),
                )
                connection.executemany("INSERT INTO choices VALUES(?,?,?)", [
                    (question["id"], n, normalize_punctuation_spacing(question["options"][str(n)]))
                    for n in range(1, 5)
                ])
                connection.execute("INSERT INTO answers VALUES(?,?,?,?,?)", (
                    question["id"], answer_choice, ids["answer_key"], answer_page, 1
                ))
                connection.executemany("INSERT INTO question_images VALUES(?,?)", [
                    (question["id"], derived_image_key(key)) for key in question["image_paths"]
                ])
                if section == "listening":
                    record = transcripts[question["number"]]
                    if not record["text"].strip():
                        raise ValueError(f"Missing dialogue for {question['id']}")
                    connection.execute("INSERT INTO transcripts VALUES(?,?,?,?,?,?)", (
                        question["id"], ids["listening_transcript"], record["pdf_page"],
                        normalize_punctuation_spacing(record["text"]), "needs_manual_review",
                        json.dumps(record.get("warnings", []), ensure_ascii=False),
                    ))

            connection.execute("INSERT INTO audio_assets VALUES(?,?,?,?,?)", (
                "035-I-B-audio", "035-I-B-listening", ids["listening_audio"], None, "not_segmented"
            ))
            connection.execute("INSERT INTO review_records(subject_type,subject_id,status,scope,evidence) "
                               "VALUES(?,?,?,?,?)", (
                                   "exam", "035-I-B", "machine_checked",
                                   "70 source PDF answer values and points vs existing preview",
                                   "70 matches; content/transcript/image review remains pending",
                               ))
            connection.executemany("INSERT INTO import_metadata VALUES(?,?)", [
                ("preview_sha256", preview_hash), ("schema_version", "pilot-1"),
                ("review_policy", "all 70 questions require human review"),
                ("extraction_correction_version", RULE_VERSION),
                ("transcript_extraction_version", "pypdf-word-spacing-v1"),
            ])
            report = check_quality(connection, fresh_import=True)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        # The existence check alone cannot protect a review DB created between
        # the check and publication. A same-directory hard link creates the
        # destination atomically and fails if another process already owns it.
        try:
            os.link(temp_filename, DB_PATH)
        except FileExistsError as exc:
            raise RuntimeError("DB appeared during import; refusing to overwrite it") from exc
        atomic_json(REPORT_PATH, report)
        return report
    finally:
        if os.path.exists(temp_filename):
            os.unlink(temp_filename)


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error) as error:
        print(f"Pilot import failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
