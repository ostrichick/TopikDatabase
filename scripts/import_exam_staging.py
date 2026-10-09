"""Fail-closed 36th TOPIK I staging validator and PostgreSQL append importer.

Read-only validation is the default. Operational writes require --apply.
"""

import argparse
import base64
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import ai_audit_35
from src.database import PostgresAuditConnection, get_database_url
from src.extraction_rules import (
    PUNCTUATION_RULE_VERSION, PUNCTUATION_RULE_VERSION_V3,
    normalize_punctuation_spacing_v2, normalize_punctuation_spacing_v3,
)

CORPUS = ROOT / "topik-past-papers"
EXAM_ID = "036-I-B"
FROZEN_35_SHA = "631c4fb71784439961956b582d0e66847eb862e2c2370f49c89d5ca520057c35"


class ImportBlocked(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ImportBlocked(message)


def sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate(data, *, allow_historical_v4=False):
    """Check source provenance, contents and complete 36-I-B shape, no DB writes."""
    require(isinstance(data, dict), "staging must be an object")
    require(data.get("exam") == {"id": EXAM_ID, "session": 36, "level": "I", "booklet": "B"},
            "unexpected exam identity")
    historical_v4 = allow_historical_v4 and data.get("extraction_version") == "pdf-first-36-v4"
    if allow_historical_v4:
        require(historical_v4, "historical punctuation exception requires 36th immutable v4")
        canonical = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        require(hashlib.sha256(canonical).hexdigest() ==
                "4c02c999b0fcbdaad48aaf9af6d0f5c754ca424f8a37885466a6704d90403684",
                "historical punctuation exception requires frozen 36th v4 SHA-256")
    else:
        source_version = data.get("extraction_version")
        require(source_version in ("pdf-first-36-v5", "pdf-first-36-v6"),
                "PDF staging requires supported extraction version pdf-first-36-v5/v6")
        expected_rule = (PUNCTUATION_RULE_VERSION_V3 if source_version == "pdf-first-36-v6"
                         else PUNCTUATION_RULE_VERSION)
        require(data.get("punctuation_rule_version") == expected_rule,
                "PDF staging requires shared punctuation normalization version")
    sources, groups, questions = data.get("sources"), data.get("groups"), data.get("questions")
    require(isinstance(sources, list) and len(sources) >= 4, "missing source files")
    require(isinstance(groups, list) and len(groups) > 0, "missing groups")
    require(isinstance(questions, list) and len(questions) == 70, "expected 70 questions")
    smap = {}
    for entry in sources:
        require(isinstance(entry, dict), "invalid source metadata")
        rel = entry.get("relative_path")
        require(isinstance(rel, str) and rel.startswith("topik-past-papers/36th/"),
                "source not in 36th corpus")
        path = (ROOT / rel).resolve()
        require(path.is_relative_to((CORPUS / "36th").resolve()) and path.is_file(),
                f"source missing/outside corpus: {rel}")
        require(rel not in smap and bool(re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256")))),
                f"duplicate/invalid source: {rel}")
        require(path.stat().st_size == entry.get("byte_size") and sha256_file(path) == entry["sha256"],
                f"source hash/size drift: {rel}")
        require(isinstance(entry.get("kind"), str) and bool(entry["kind"]), "missing source kind")
        smap[rel] = entry
    for needed in ("test_paper", "answer_key", "listening_audio", "listening_transcript"):
        require(any(s["kind"] == needed for s in sources), f"missing {needed}")
    gmap = {}
    for group in groups:
        require(isinstance(group, dict), "invalid group")
        gid = group.get("id")
        require(isinstance(gid, str) and gid.startswith("036-I-") and gid not in gmap,
                f"invalid/duplicate group ID: {gid}")
        first, last = group.get("first_exam_number"), group.get("last_exam_number")
        require(isinstance(first, int) and isinstance(last, int) and 1 <= first <= last <= 70,
                f"invalid group range: {gid}")
        require(group.get("section") in ("listening", "reading") and
                isinstance(group.get("instruction", ""), str) and
                isinstance(group.get("passage_text", ""), str), f"invalid group content: {gid}")
        gmap[gid] = group
    expected = {f"036-I-L-{n:03d}" for n in range(1, 31)} | {
        f"036-I-R-{n:03d}" for n in range(31, 71)}
    ids = [q.get("id") for q in questions if isinstance(q, dict)]
    require(len(ids) == 70 and len(set(ids)) == 70 and set(ids) == expected,
            "question IDs must cover 1-30 listening and 31-70 reading")
    points, images, image_links, transcribed = Counter(), {}, 0, 0
    for q in questions:
        qid, n = q["id"], q["exam_number"]
        section = "listening" if "-L-" in qid else "reading"
        require(q.get("section") == section and isinstance(n, int) and
                qid == f"036-I-{'L' if section == 'listening' else 'R'}-{n:03d}",
                f"section/number mismatch: {qid}")
        require(q.get("answer_key_number") == n,
                f"36th printed answer numbers are global 1-70: {qid}")
        gid = q.get("group_id")
        require(gid in gmap and gmap[gid]["section"] == section and
                gmap[gid]["first_exam_number"] <= n <= gmap[gid]["last_exam_number"],
                f"group not found or wrong: {qid}")
        require(q.get("source_relative_path") in smap and
                smap[q["source_relative_path"]]["kind"] in ("test_paper", f"{section}_paper") and
                isinstance(q.get("source_pdf_page"), int) and q["source_pdf_page"] > 0,
                f"question paper provenance invalid: {qid}")
        require(isinstance(q.get("stem"), str) and isinstance(q.get("raw_question_text"), str),
                f"missing source question text: {qid}")
        require(not re.search(r"[①②③④]\s*$", q["stem"]),
                f"choice-number marker leaked into stem boundary: {qid}")
        choices = q.get("choices")
        require(isinstance(choices, list) and len(choices) == 4 and
                {ch.get("number") for ch in choices if isinstance(ch, dict)} == {1, 2, 3, 4} and
                all(isinstance(ch.get("text"), str) for ch in choices),
                f"missing/duplicate choice slots: {qid}")
        answer = q.get("answer")
        require(isinstance(answer, dict) and answer.get("choice_number") in (1, 2, 3, 4) and
                answer.get("source_pdf_page") in (1, 2), f"invalid answer: {qid}")
        require(q.get("points") in (2, 3, 4), f"invalid score: {qid}")
        points[section] += q["points"]
        pics = q.get("images", [])
        require(isinstance(pics, list) and bool(pics) == bool(q.get("requires_image")),
                f"image requirement mismatch: {qid}")
        for pic in pics:
            require(isinstance(pic, dict) and isinstance(pic.get("bytes_base64"), str) and
                    pic.get("source_relative_path") in smap and
                    pic.get("mime_type") in ("image/png", "image/jpeg"), f"bad image: {qid}")
            try:
                binary = base64.b64decode(pic["bytes_base64"], validate=True)
            except Exception as exc:
                raise ImportBlocked(f"bad image bytes: {qid}") from exc
            require(pic.get("sha256") == hashlib.sha256(binary).hexdigest() and
                    isinstance(pic.get("key"), str) and
                    (pic["key"].startswith("036-I-") or pic["key"].startswith("assets/036-I-")),
                    f"bad image hash/key: {qid}")
            val = (pic["sha256"], pic["mime_type"], binary)
            require(pic["key"] not in images or images[pic["key"]] == val,
                    f"shared image key conflict: {pic['key']}")
            images[pic["key"]] = val
            image_links += 1
        transcript = q.get("transcript")
        if section == "listening":
            require(isinstance(transcript, dict) and
                    isinstance(transcript.get("dialogue_text"), str) and
                    bool(transcript["dialogue_text"].strip()) and
                    transcript.get("source_relative_path") in smap and
                    smap[transcript["source_relative_path"]]["kind"] == "listening_transcript" and
                    isinstance(transcript.get("source_pdf_page"), int) and
                    transcript["source_pdf_page"] > 0,
                    f"missing source listening transcript: {qid}")
            transcribed += 1
        else:
            require(transcript is None, f"reading has unexpected transcript: {qid}")
    require(points == {"listening": 100, "reading": 100}, f"score total mismatch: {points}")
    require(transcribed == 30, "expected 30 transcripts")
    # The original raw_question_text is immutable extraction evidence and is
    # intentionally exempt. Every persisted, human-readable text field must
    # pass the shared punctuation stage before a new session can be imported.
    text_fields = []
    for g in groups:
        text_fields.extend((f"{g['id']}.{field}", g.get(field, ""))
                           for field in ("instruction", "passage_text"))
    for q in questions:
        text_fields.append((f"{q['id']}.stem", q["stem"]))
        text_fields.extend((f"{q['id']}.choice[{c['number']}]", c["text"])
                           for c in q["choices"])
        if q.get("transcript") is not None:
            text_fields.append((f"{q['id']}.transcript", q["transcript"]["dialogue_text"]))
    normalizer = (normalize_punctuation_spacing_v3
                  if data.get("extraction_version") == "pdf-first-36-v6"
                  else normalize_punctuation_spacing_v2)
    residual = [scope for scope, value in text_fields
                if normalizer(value) != value]
    require(not residual or historical_v4,
            "unresolved punctuation spacing: " + ", ".join(residual[:12]))
    warnings = data.get("warnings", [])
    require(isinstance(warnings, list), "warnings must be a list")
    all_warnings = []
    scopes = [("exam", warnings)]
    scopes.extend((q["id"], q.get("warnings", [])) for q in questions)
    scopes.extend((q["id"] + ":transcript", q["transcript"].get("warnings", []))
                  for q in questions if q.get("transcript") is not None)
    scopes.extend((g["id"] + ":group", g.get("warnings", [])) for g in groups)
    for scope, entries in scopes:
        require(isinstance(entries, list), f"warnings for {scope} must be a list")
        for warning in entries:
            require(isinstance(warning, dict) and
                    warning.get("severity") in ("info", "review", "low", "medium", "high", "critical", "blocking") and
                    isinstance(warning.get("code"), str) and bool(warning["code"]) and
                    isinstance(warning.get("message"), str) and bool(warning["message"]),
                    f"malformed/unclassified warning for {scope}; fail closed")
            all_warnings.append({"scope": scope, **warning})
    blocked = [w for w in all_warnings if w["severity"] in ("high", "critical", "blocking")]
    return {
        "status": "blocked" if blocked else "validated",
        "exam_id": EXAM_ID, "questions": 70, "choices": 280,
        "answers": 70, "transcripts": transcribed,
        "groups": len(gmap), "image_assets": len(images), "image_links": image_links,
        "points_by_section": dict(points), "sources": len(sources),
        "warnings": len(all_warnings), "blocking_warnings": blocked,
        "staging_sha256": hashlib.sha256(json.dumps(
            data, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest(),
    }


def apply(data):
    """Insert a NEW exam in one PostgreSQL transaction, or rollback all rows."""
    report = validate(data)
    require(report["status"] == "validated", "blocking extraction warnings: no import")
    db = PostgresAuditConnection(get_database_url(required=True), readonly=False)
    try:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        db.execute(
            "LOCK TABLE exams,source_files,sections,question_groups,questions,choices,"
            "answers,images,question_images,transcripts,audio_assets,audio_segments "
            "IN SHARE ROW EXCLUSIVE MODE"
        )
        require(db.execute("SELECT id FROM exams WHERE id=?", (EXAM_ID,)).fetchone() is None,
                "36th exam already exists; no overwrite")
        require(ai_audit_35._sha(ai_audit_35._source_snapshot_payload(db)) == FROZEN_35_SHA,
                "existing 35th audit source snapshot differs")
        db.execute("INSERT INTO exams(id,session,level,booklet) VALUES(?,?,?,?)",
                   (EXAM_ID, 36, "I", "B"))
        for section, first, last in (("listening", 1, 30), ("reading", 31, 70)):
            db.execute(
                "INSERT INTO sections(id,exam_id,name,first_exam_number,last_exam_number,answer_key_offset) "
                "VALUES(?,?,?,?,?,0)",
                (f"036-I-B-{section}", EXAM_ID, section, first, last),
            )
        source_ids = {}
        for s in data["sources"]:
            row = db.execute(
                "INSERT INTO source_files(relative_path,kind,sha256,byte_size,source_url,source_page) "
                "VALUES(?,?,?,?,?,?) RETURNING id",
                (s["relative_path"], s["kind"], s["sha256"], s["byte_size"],
                 s.get("source_url"), s.get("source_page")),
            ).fetchone()
            source_ids[s["relative_path"]] = row[0]
        for g in data["groups"]:
            db.execute(
                "INSERT INTO question_groups(id,section_id,first_exam_number,last_exam_number,"
                "instruction,passage_text,points_each,passage_image_key) VALUES(?,?,?,?,?,?,?,NULL)",
                (g["id"], f"036-I-B-{g['section']}", g["first_exam_number"],
                 g["last_exam_number"], g.get("instruction", ""),
                 g.get("passage_text", ""), g.get("points_each")),
            )
        answer_path = next(s["relative_path"] for s in data["sources"] if s["kind"] == "answer_key")
        linked_images = set()
        pending_choices = []
        for q in data["questions"]:
            db.execute(
                "INSERT INTO questions(id,section_id,group_id,source_file_id,exam_number,"
                "answer_key_number,source_pdf_page,printed_page,points,stem,raw_question_text,"
                "passage_id,requires_image,review_status,extraction_origin,preview_flags_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,'needs_manual_review',?,?)",
                (q["id"], f"036-I-B-{q['section']}", q["group_id"],
                 source_ids[q["source_relative_path"]], q["exam_number"], q["answer_key_number"],
                 q["source_pdf_page"], q.get("printed_page"), q["points"], q["stem"],
                 q["raw_question_text"], int(bool(q["images"])),
                 data.get("extraction_version", "pdf-first-36-v1"),
                 json.dumps(q.get("warnings", []), ensure_ascii=False)),
            )
            for c in q["choices"]:
                pending_choices.append((q["id"], c["number"], c["text"]))
            db.execute(
                "INSERT INTO answers(question_id,choice_number,source_file_id,source_pdf_page,"
                "preview_and_pdf_agree) VALUES(?,?,?,?,0)",
                (q["id"], q["answer"]["choice_number"], source_ids[answer_path],
                 q["answer"]["source_pdf_page"]),
            )
            for img in q["images"]:
                if img["key"] not in linked_images:
                    db.execute(
                        "INSERT INTO images(key,mime_type,sha256,bytes,source_file_id) "
                        "VALUES(?,?,?,?,?)",
                        (img["key"], img["mime_type"], img["sha256"],
                         base64.b64decode(img["bytes_base64"], validate=True),
                         source_ids[img["source_relative_path"]]),
                    )
                    linked_images.add(img["key"])
                db.execute("INSERT INTO question_images(question_id,image_key) VALUES(?,?)",
                           (q["id"], img["key"]))
            if q["section"] == "listening":
                t = q["transcript"]
                db.execute(
                    "INSERT INTO transcripts(question_id,source_file_id,source_pdf_page,dialogue_text,"
                    "review_status,warnings_json) VALUES(?,?,?,?,'needs_manual_review',?)",
                    (q["id"], source_ids[t["source_relative_path"]], t["source_pdf_page"],
                     t["dialogue_text"], json.dumps(t.get("warnings", []), ensure_ascii=False)),
                )
        db.executemany("INSERT INTO choices(question_id,number,text) VALUES(?,?,?)",
                       pending_choices)
        audio_path = next(s["relative_path"] for s in data["sources"] if s["kind"] == "listening_audio")
        db.execute(
            "INSERT INTO audio_assets(id,section_id,source_file_id,duration_seconds,timing_status) "
            "VALUES(?,?,?,NULL,'not_segmented')",
            ("036-I-B-audio", "036-I-B-listening", source_ids[audio_path]),
        )
        db.execute("INSERT INTO import_metadata(key,value) VALUES(?,?)",
                   ("036-I-B:staging_sha256", report["staging_sha256"]))
        rows = db.execute(
            "SELECT s.name,COUNT(*) FROM questions q JOIN sections s ON q.section_id=s.id "
            "WHERE s.exam_id=? GROUP BY s.name", (EXAM_ID,),
        ).fetchall()
        require(dict(rows) == {"listening": 30, "reading": 40}, "inserted question count mismatch")
        require(ai_audit_35._sha(ai_audit_35._source_snapshot_payload(db)) == FROZEN_35_SHA,
                "35th snapshot drift: rollback all")
        validate(data)  # Guard against source file replacement before commit.
        db.commit()
        return {**report, "status": "applied", "35th_unchanged": True, "36th_pending_review": 70}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--staging", type=Path, required=True)
    cli.add_argument("--apply", action="store_true")
    args = cli.parse_args(argv)
    try:
        path = args.staging.resolve()
        require(path.is_file() and path.is_relative_to(CORPUS.resolve()),
                "staging must be inside local ignored corpus")
        data = json.loads(path.read_text(encoding="utf-8"))
        result = apply(data) if args.apply else validate(data)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result["status"] != "blocked" else 1
    except (OSError, KeyError, ValueError, ImportBlocked) as exc:
        print(f"36th staging import blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
