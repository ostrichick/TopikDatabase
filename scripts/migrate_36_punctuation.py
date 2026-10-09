"""Safely migrate the audited 036-I-B punctuation spacing from v4 to v5.

The default operation is a read-only PostgreSQL preflight. --apply makes one
transactional change to existing 36th-session text only. It never imports an
exam, mutates 35th-session state, or changes human review history/statuses.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.import_exam_staging import EXAM_ID, FROZEN_35_SHA, validate
from src import ai_audit_35
from src.database import connect_postgres, get_database_url
from src.extraction_rules import PUNCTUATION_RULE_VERSION, normalize_punctuation_spacing_v2

CORPUS = ROOT / "topik-past-papers" / "derived" / EXAM_ID
V4 = CORPUS / "staging-v4.json"
V5 = CORPUS / "staging-v5.json"
FROZEN_V4_FILE_SHA256 = "4c02c999b0fcbdaad48aaf9af6d0f5c754ca424f8a37885466a6704d90403684"
FROZEN_V5_FILE_SHA256 = "af72d0c340fe7b833b63dc0eb07652538e3dd292668a6e4986b4247792e9e095"
MIGRATION_KEY = "036-I-B:punctuation:v4-to-v5"
EXPECTED_COUNTS = {"questions.stem": 15, "choices.text": 8,
                   "question_groups.passage_text": 9, "transcripts.dialogue_text": 22}
EXPECTED_SPACES = 138  # includes source-verified R51-52 numeric sentence boundary
MUTABLE_FIELDS = {
    "questions": ("stem",),
    "choices": ("text",),
    "question_groups": ("instruction", "passage_text"),
    "transcripts": ("dialogue_text",),
}


class MigrationBlocked(RuntimeError):
    """An explicit safety gate failed. No partial changes may be committed."""


def require(test: bool, why: str) -> None:
    if not test:
        raise MigrationBlocked(why)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                   default=str).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class Change:
    table: str
    row_id: str
    field: str
    before: str
    after: str
    number: int | None = None

    def evidence(self) -> dict[str, Any]:
        return {
            "table": self.table, "id": self.row_id, "number": self.number,
            "field": self.field, "before": self.before, "after": self.after,
        }


def read_staging(path: Path, *, frozen_sha: str | None = None) -> tuple[dict, str]:
    resolved = path.resolve()
    require(resolved.is_relative_to(CORPUS.resolve()) and resolved.is_file(),
            "staging must be an existing file inside derived/036-I-B")
    raw = resolved.read_bytes()
    file_sha = hashlib.sha256(raw).hexdigest()
    if frozen_sha is not None:
        require(file_sha == frozen_sha, "immutable v4 raw file hash differs")
    return json.loads(raw.decode("utf-8")), file_sha


def build_changes(before: dict, after: dict) -> list[Change]:
    """Require v5 = v4 + precisely the expected punctuation spaces/version."""
    require(before.get("exam") == {"id": EXAM_ID, "session": 36, "level": "I", "booklet": "B"},
            "v4 exam identity differs")
    require(before.get("extraction_version") == "pdf-first-36-v4",
            "unrecognized frozen v4 extraction version")
    require(isinstance(after.get("extraction_version"), str)
            and after["extraction_version"] == "pdf-first-36-v5",
            "v5 extraction version is missing/incorrect")
    require(after.get("punctuation_rule_version") == PUNCTUATION_RULE_VERSION,
            "v5 punctuation rule version is missing/incorrect")
    require("punctuation_rule_version" not in before,
            "v4 source unexpectedly contains a derived rule version")
    require(len(before.get("questions", [])) == 70 and len(before.get("groups", [])) == 26,
            "v4 collection size is unexpected")
    require(len(after.get("questions", [])) == 70 and len(after.get("groups", [])) == 26,
            "v5 collection size is unexpected")
    canonical = copy.deepcopy(before)
    canonical["extraction_version"] = after["extraction_version"]
    canonical["punctuation_rule_version"] = after["punctuation_rule_version"]
    changes: list[Change] = []

    def record(table: str, row_id: str, field: str, prior: str, updated: str,
               number: int | None = None):
        require(isinstance(prior, str) and isinstance(updated, str),
                f"malformed {table}.{field}: {row_id}")
        require(updated == normalize_punctuation_spacing_v2(prior),
                f"v5 contains edits other than the audited punctuation rule: {table}.{field} {row_id}")
        if prior != updated:
            changes.append(Change(table, row_id, field, prior, updated, number))

    for old_q, new_q, target_q in zip(
        before["questions"], after["questions"], canonical["questions"], strict=True
    ):
        qid = old_q["id"]
        require(new_q.get("id") == qid, f"question ordering/identity changed: {qid}")
        record("questions", qid, "stem", old_q["stem"], new_q["stem"])
        target_q["stem"] = new_q["stem"]
        require(len(old_q["choices"]) == len(new_q["choices"]) == 4,
                f"choice shape changed: {qid}")
        for old_c, new_c, target_c in zip(
            old_q["choices"], new_q["choices"], target_q["choices"], strict=True
        ):
            number = old_c["number"]
            require(new_c.get("number") == number, f"choice identity changed: {qid}/{number}")
            record("choices", qid, "text", old_c["text"], new_c["text"], number)
            target_c["text"] = new_c["text"]
        if old_q.get("transcript") is not None:
            require(new_q.get("transcript") is not None, f"transcript removed: {qid}")
            prior = old_q["transcript"]["dialogue_text"]
            updated = new_q["transcript"]["dialogue_text"]
            record("transcripts", qid, "dialogue_text", prior, updated)
            target_q["transcript"]["dialogue_text"] = updated

    for old_g, new_g, target_g in zip(
        before["groups"], after["groups"], canonical["groups"], strict=True
    ):
        gid = old_g["id"]
        require(new_g.get("id") == gid, f"group identity changed: {gid}")
        for field in MUTABLE_FIELDS["question_groups"]:
            record("question_groups", gid, field, old_g[field], new_g[field])
            target_g[field] = new_g[field]

    require(canonical == after,
            "v5 has changes outside permitted text fields/extraction version (including raw text)")
    counts = dict(Counter(f"{c.table}.{c.field}" for c in changes))
    require(counts == EXPECTED_COUNTS,
            f"correction field counts differ from audited 54 fields: {counts}")
    spaces = sum(len(c.after) - len(c.before) for c in changes)
    require(spaces == EXPECTED_SPACES, f"inserted punctuation spaces differ: {spaces}")
    return changes


def _text_rows(conn, *, lock: bool) -> dict[tuple[str, str, int | None, str], str]:
    """Read all 36th text, including unaffected fields, to detect manual edits."""
    suffix = " FOR UPDATE" if lock else ""
    rows = {}
    queries = (
        ("questions", "id", None, "stem,raw_question_text",
         "SELECT id,stem,raw_question_text FROM questions WHERE section_id IN "
         "(SELECT id FROM sections WHERE exam_id=%s) ORDER BY id" + suffix),
        ("choices", "question_id", "number", "text",
         "SELECT question_id,number,text FROM choices WHERE question_id IN "
         "(SELECT id FROM questions WHERE section_id IN "
         "(SELECT id FROM sections WHERE exam_id=%s)) ORDER BY question_id,number" + suffix),
        ("question_groups", "id", None, "instruction,passage_text",
         "SELECT id,instruction,passage_text FROM question_groups WHERE section_id IN "
         "(SELECT id FROM sections WHERE exam_id=%s) ORDER BY id" + suffix),
        ("transcripts", "question_id", None, "dialogue_text",
         "SELECT question_id,dialogue_text FROM transcripts WHERE question_id IN "
         "(SELECT id FROM questions WHERE section_id IN "
         "(SELECT id FROM sections WHERE exam_id=%s)) ORDER BY question_id" + suffix),
    )
    for table, key, number_col, fields, sql in queries:
        for item in conn.execute(sql, (EXAM_ID,)).fetchall():
            for field in fields.split(","):
                rows[(table, item[key], item[number_col] if number_col else None, field)] = item[field]
    return rows


def _staging_rows(data: dict) -> dict[tuple[str, str, int | None, str], str]:
    rows = {}
    for q in data["questions"]:
        qid = q["id"]
        for field in ("stem", "raw_question_text"):
            rows[("questions", qid, None, field)] = q[field]
        for choice in q["choices"]:
            rows[("choices", qid, choice["number"], "text")] = choice["text"]
        if q.get("transcript") is not None:
            rows[("transcripts", qid, None, "dialogue_text")] = q["transcript"]["dialogue_text"]
    for group in data["groups"]:
        for field in MUTABLE_FIELDS["question_groups"]:
            rows[("question_groups", group["id"], None, field)] = group[field]
    return rows


def _review_state(conn) -> str:
    # Includes all 36th history, review decisions and transcript statuses.
    records = [
        dict(row) for row in conn.execute(
            "SELECT r.id,r.subject_type,r.subject_id,r.status,r.reviewer,r.scope,r.evidence,"
            "r.reviewed_at FROM review_records r WHERE r.subject_id IN "
            "(SELECT id FROM questions WHERE section_id IN "
            "(SELECT id FROM sections WHERE exam_id=%s)) ORDER BY r.id", (EXAM_ID,)
        ).fetchall()
    ]
    questions = [dict(row) for row in conn.execute(
        "SELECT id,review_status FROM questions WHERE section_id IN "
        "(SELECT id FROM sections WHERE exam_id=%s) ORDER BY id", (EXAM_ID,)
    ).fetchall()]
    transcripts = [dict(row) for row in conn.execute(
        "SELECT question_id,review_status FROM transcripts WHERE question_id IN "
        "(SELECT id FROM questions WHERE section_id IN "
        "(SELECT id FROM sections WHERE exam_id=%s)) ORDER BY question_id", (EXAM_ID,)
    ).fetchall()]
    require(len(questions) == 70 and len(transcripts) == 30,
            "36th human review inventory is incomplete")
    require(all(q["review_status"] == "needs_manual_review" for q in questions)
            and all(t["review_status"] == "needs_manual_review" for t in transcripts),
            "36th contains reviewed questions/transcripts; preserve manual work and stop")
    require(not records, "36th already has human review records; source correction requires reconciliation")
    return digest({"questions": questions, "transcripts": transcripts, "history": records})


def _update(conn, change: Change) -> None:
    key_column = "question_id" if change.table in ("choices", "transcripts") else "id"
    sql = f"UPDATE {change.table} SET {change.field}=%s WHERE {key_column}=%s AND {change.field}=%s"
    params: tuple = (change.after, change.row_id, change.before)
    if change.table == "choices":
        sql += " AND number=%s"
        params += (change.number,)
    require(conn.execute(sql, params).rowcount == 1,
            f"concurrent change blocked at {change.table}/{change.row_id}/{change.field}")


def run(before: dict, after: dict, v4_sha: str, v5_sha: str, *,
        apply: bool = False, url: str | None = None) -> dict:
    changes = build_changes(before, after)
    spaces = sum(len(c.after) - len(c.before) for c in changes)
    expected_before, expected_after = _staging_rows(before), _staging_rows(after)
    migration_evidence = {
        "version": "036-I-B-punctuation-v4-to-v5",
        "v4_sha256": v4_sha, "v5_sha256": v5_sha,
        "changes_sha256": digest([c.evidence() for c in changes]),
        "changed_cells": len(changes),
        "inserted_spaces": spaces,
    }
    marker = json.dumps(migration_evidence, ensure_ascii=False, sort_keys=True)
    conn = connect_postgres(url or get_database_url(required=True), readonly=not apply)
    try:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        if apply:
            # Serializes migration with review UI writes and all competing migrations.
            # Locks are acquired in canonical table order before snapshot checks.
            conn.execute(
                "LOCK TABLE exams,source_files,sections,question_groups,questions,"
                "choices,answers,images,question_images,transcripts,audio_assets,"
                "audio_segments,review_records,import_metadata IN SHARE ROW EXCLUSIVE MODE"
            )
        require(conn.execute(
            "SELECT id FROM exams WHERE id=%s AND session=36 AND level='I' AND booklet='B'",
            (EXAM_ID,)
        ).fetchone() is not None, "36th exam is not imported")
        before_35 = _snapshot_35(conn)
        require(before_35 == FROZEN_35_SHA, "35th frozen source snapshot mismatch")
        review_before = _review_state(conn)
        marker_row = conn.execute(
            "SELECT value FROM import_metadata WHERE key=%s" + (" FOR UPDATE" if apply else ""),
            (MIGRATION_KEY,)
        ).fetchone()
        actual = _text_rows(conn, lock=apply)
        require(set(actual) == set(expected_before),
                "36th rows/fields differ from v4; new/missing rows or mapping")
        if marker_row is None:
            require(actual == expected_before,
                    "36th PostgreSQL text differs from v4; manual edits or partial migration detected")
            state = "pending"
        else:
            require(marker_row["value"] == marker,
                    "existing migration marker differs from this exact v4/v5 evidence")
            require(actual == expected_after,
                    "migration marker is present but 36th text differs from v5")
            state = "already_applied"
        if apply and state == "pending":
            for change in changes:
                _update(conn, change)
            conn.execute("INSERT INTO import_metadata(key,value) VALUES(%s,%s)",
                         (MIGRATION_KEY, marker))
            require(_text_rows(conn, lock=False) == expected_after,
                    "post-migration text mismatch")
            require(_review_state(conn) == review_before,
                    "36th human review statuses/history changed during migration")
            require(_snapshot_35(conn) == before_35, "35th source snapshot changed")
            conn.commit()
            state = "applied"
        else:
            conn.rollback()
        return {
            "status": state, "mode": "apply" if apply else "dry_run",
            "exam_id": EXAM_ID, "changed_cells": len(changes),
            "inserted_spaces": spaces, "35th_source_sha256": before_35,
            "36th_review_state_sha256": review_before, "migration": migration_evidence,
            "changes": [c.evidence() for c in changes],
        }
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


class _TupleAdapter:
    """Supply the existing 35th-source snapshot helper with tuple rows."""

    def __init__(self, connection):
        self._connection = connection
        self.backend = "postgres"
        self.ai_audit_tuple_rows = True

    def execute(self, sql, params=()):
        from psycopg.rows import tuple_row
        cursor = self._connection.cursor(row_factory=tuple_row)
        cursor.execute(sql.replace("?", "%s"), params)
        return cursor


class _Rows:
    """A cursor-compatible immutable in-memory result for cached PG reads."""

    def __init__(self, items):
        self.items = list(items)

    def __iter__(self):
        return iter(self.items)

    def fetchall(self):
        return list(self.items)

    def fetchone(self):
        return self.items[0] if self.items else None


class _CachedSnapshotAdapter(_TupleAdapter):
    """Batch the original 35th snapshot's per-question lookups over SSH.

    Reuses its canonical serializer and SHA unchanged. The four cached query
    groups replace ~280 one-row round trips with four scoped PostgreSQL reads.
    """

    def __init__(self, connection):
        super().__init__(connection)
        from psycopg.rows import tuple_row
        self.cache: dict[str, dict[str, list[tuple]]] = {}
        batches = {
            "transcripts": (
                "SELECT t.question_id,t.source_file_id,t.source_pdf_page,"
                "t.dialogue_text,t.warnings_json FROM transcripts t "
                "JOIN questions q ON q.id=t.question_id "
                "JOIN sections s ON s.id=q.section_id "
                "WHERE s.exam_id=%s ORDER BY t.question_id"
            ),
            "images": (
                "SELECT qi.question_id,i.key,i.sha256,i.mime_type,i.source_file_id "
                "FROM question_images qi JOIN images i ON i.key=qi.image_key "
                "JOIN questions q ON q.id=qi.question_id "
                "JOIN sections s ON s.id=q.section_id "
                "WHERE s.exam_id=%s ORDER BY qi.question_id,i.key"
            ),
            "audio": (
                "SELECT a.question_id,a.start_ms,a.end_ms,a.source_sha256,"
                "aa.source_file_id FROM audio_segments a "
                "JOIN audio_assets aa ON aa.id=a.audio_asset_id "
                "JOIN questions q ON q.id=a.question_id "
                "JOIN sections s ON s.id=q.section_id "
                "WHERE s.exam_id=%s ORDER BY a.question_id"
            ),
            "choices": (
                "SELECT c.question_id,c.number,c.text FROM choices c "
                "JOIN questions q ON q.id=c.question_id "
                "JOIN sections s ON s.id=q.section_id "
                "WHERE s.exam_id=%s ORDER BY c.question_id,c.number"
            ),
        }
        for kind, sql in batches.items():
            grouped: dict[str, list[tuple]] = {}
            with connection.cursor(row_factory=tuple_row) as cursor:
                cursor.execute(sql, ("035-I-B",))
                for row in cursor.fetchall():
                    grouped.setdefault(row[0], []).append(row[1:])
            self.cache[kind] = grouped

    def execute(self, sql, params=()):
        prefix_kind = (
            ("SELECT source_file_id,source_pdf_page,dialogue_text,warnings_json "
             "FROM transcripts WHERE question_id=?", "transcripts"),
            ("SELECT i.key,i.sha256,i.mime_type,i.source_file_id "
             "FROM question_images qi ", "images"),
            ("SELECT a.start_ms,a.end_ms,a.source_sha256,aa.source_file_id "
             "FROM audio_segments a ", "audio"),
            ("SELECT number,text FROM choices WHERE question_id=?", "choices"),
        )
        for prefix, kind in prefix_kind:
            if sql.startswith(prefix):
                require(len(params) == 1, f"unexpected 35th snapshot parameter count: {kind}")
                return _Rows(self.cache[kind].get(params[0], ()))
        return super().execute(sql, params)


def _snapshot_35(conn) -> str:
    return ai_audit_35._sha(ai_audit_35._source_snapshot_payload(_CachedSnapshotAdapter(conn)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v4", type=Path, default=V4)
    parser.add_argument("--v5", type=Path, default=V5)
    parser.add_argument("--apply", action="store_true", help="Commit the reviewed 36th changes")
    args = parser.parse_args(argv)
    try:
        before, before_sha = read_staging(args.v4, frozen_sha=FROZEN_V4_FILE_SHA256)
        after, after_sha = read_staging(args.v5, frozen_sha=FROZEN_V5_FILE_SHA256)
        build_changes(before, after)
        # The approved v4 file needs the historical read-only exception to the
        # punctuation gate. All other provenance and shape checks remain active.
        require(validate(before, allow_historical_v4=True)["status"] == "validated",
                "v4 historical staging validation blocked")
        require(validate(after)["status"] == "validated", "v5 staging validation blocked")
        result = run(before, after, before_sha, after_sha, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (MigrationBlocked, ValueError, KeyError, OSError, RuntimeError) as exc:
        print(f"36th punctuation migration blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
