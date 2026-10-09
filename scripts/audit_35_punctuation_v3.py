"""Read-only 35th TOPIK I B punctuation-v3 display-overlay candidate audit.

This tool NEVER updates a source database or review state. It accepts the
operational TOPIK_DATABASE_URL or an explicitly named frozen SQLite archive.
It emits an ignored derived JSON only if all historical 35th preconditions
and the expected field-level correction scope match exactly.

From the repository root:
  py -3 -m scripts.audit_35_punctuation_v3
  py -3 -m scripts.audit_35_punctuation_v3 --sqlite-db topik-past-papers/derived/035-I-B.sqlite
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.database import get_database_url, sqlite_readonly
from src.extraction_rules import PUNCTUATION_RULE_VERSION_V3, normalize_punctuation_spacing_v3
from src.review_ui import ReviewStore

EXAM_ID = "035-I-B"
DEFAULT_REPORT = ROOT / "topik-past-papers" / "derived" / "035-I-B-punctuation-v3-overlay-candidates.json"
EXPECTED_COUNTS = {"question_groups": 26, "questions": 70, "choices": 280, "transcripts": 30}
EXPECTED_CHANGE_KEYS = frozenset({
    ("question_groups", "035-I-L-07-10", "instruction"),
    ("question_groups", "035-I-L-11-14", "instruction"),
    ("transcripts", "035-I-L-001", "dialogue_text"),
})
EXPECTED_ADDED_SPACES = 4
READ_SQL = {
    "question_groups": (
        "SELECT g.id AS record_id, g.instruction, g.passage_text "
        "FROM question_groups g JOIN sections s ON s.id=g.section_id "
        "WHERE s.exam_id=? ORDER BY g.id"
    ),
    "questions": (
        "SELECT q.id AS record_id, q.stem FROM questions q "
        "JOIN sections s ON s.id=q.section_id "
        "WHERE s.exam_id=? ORDER BY q.id"
    ),
    "choices": (
        "SELECT c.question_id AS question_id, c.number AS choice_number, c.text "
        "FROM choices c JOIN questions q ON q.id=c.question_id "
        "JOIN sections s ON s.id=q.section_id "
        "WHERE s.exam_id=? ORDER BY c.question_id,c.number"
    ),
    "transcripts": (
        "SELECT t.question_id AS record_id, t.dialogue_text "
        "FROM transcripts t JOIN questions q ON q.id=t.question_id "
        "JOIN sections s ON s.id=q.section_id "
        "WHERE s.exam_id=? ORDER BY t.question_id"
    ),
}
DISPLAY_FIELDS = {
    "question_groups": ("instruction", "passage_text"),
    "questions": ("stem",),
    "choices": ("text",),
    "transcripts": ("dialogue_text",),
}


class AuditBlocked(RuntimeError):
    """Input identity, expected correction scope or non-mutating output gate failed."""


def require(condition: bool, explanation: str) -> None:
    if not condition:
        raise AuditBlocked(explanation)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _added_spaces_only(before: str, after: str) -> int:
    """Prove that after differs by ASCII-space insertions only."""
    source = target = additions = 0
    while target < len(after):
        if source < len(before) and before[source] == after[target]:
            source += 1
            target += 1
        elif after[target] == " ":
            additions += 1
            target += 1
        else:
            raise AuditBlocked("v3 tried to change characters other than inserting ASCII spaces")
    require(source == len(before), "v3 tried to remove source characters")
    return additions


def _preview(before: str, after: str) -> dict:
    """Return bounded evidence around the first insertion, preserving full hashes."""
    location = next((i for i, ch in enumerate(after)
                     if i >= len(before) or ch != before[i]), 0)
    start = max(0, location - 28)
    end = location + 70
    return {
        "before": before[start:end],
        "after": after[start:end],
        "source_sha256": _sha256(before),
        "overlay_sha256": _sha256(after),
    }


@contextmanager
def _readonly_store(exam_id: str, *, sqlite_db: Path | None = None,
                    database_url: str | None = None):
    require(exam_id == EXAM_ID, "35th overlay audit is restricted to 035-I-B")
    require(not (sqlite_db is not None and database_url is not None),
            "choose either SQLite or PostgreSQL")
    if sqlite_db is not None:
        store = ReviewStore(db_path=sqlite_db, exam_id=exam_id)
        # SQLite PRAGMA query_only plus a consistent transaction prevents
        # even an accidental UPDATE through this scanner's connection.
        with sqlite_readonly(store.db_path) as db:
            yield "sqlite", db
    else:
        url = database_url if database_url is not None else get_database_url(required=True)
        store = ReviewStore(database_url=url, exam_id=exam_id)
        with closing(store._connect()) as db:
            # The standard PostgresReadConnection defaults to read-only.
            # Explicitly fix snapshot semantics for all five SELECT queries.
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield "postgres", db


def scan(db, *, exam_id: str = EXAM_ID, backend: str = "postgres") -> dict:
    """Scan only the requested exam, returning an in-memory overlay proposal."""
    require(exam_id == EXAM_ID, "wrong exam: expected 035-I-B")
    exam = db.execute(
        "SELECT id,session,level,booklet FROM exams WHERE id=?", (exam_id,)
    ).fetchone()
    require(exam is not None, "35th exam not found")
    require((exam["id"], exam["session"], exam["level"], exam["booklet"])
            == (EXAM_ID, 35, "I", "B"), "35th exam identity mismatch")

    counts = {}
    inspected = []
    candidates = []
    for table, query in READ_SQL.items():
        rows = db.execute(query, (EXAM_ID,)).fetchall()
        counts[table] = len(rows)
        for row in rows:
            row = dict(row)
            record_id = row.get("record_id", row.get("question_id"))
            require(isinstance(record_id, str) and record_id.startswith("035-I-"),
                    "cross-exam record found")
            if table == "choices":
                require(row["choice_number"] in (1, 2, 3, 4), "invalid choice number")
                record_key = f"{record_id}/choice-{row['choice_number']}"
            else:
                record_key = record_id
            for field in DISPLAY_FIELDS[table]:
                original = row[field]
                require(isinstance(original, str), "unexpected null/nontext display field")
                normalized = normalize_punctuation_spacing_v3(original)
                require(normalize_punctuation_spacing_v3(normalized) == normalized,
                        "v3 punctuation overlay must be idempotent")
                fingerprint = f"{table}/{record_key}/{field}"
                inspected.append([fingerprint, _sha256(original)])
                if original == normalized:
                    continue
                added = _added_spaces_only(original, normalized)
                require(added == len(normalized) - len(original) and added > 0,
                        "v3 correction must add spaces only")
                candidates.append({
                    "table": table, "id": record_id, "field": field,
                    **({"choice_number": row["choice_number"]} if table == "choices" else {}),
                    "spaces_added": added,
                    "preview": _preview(original, normalized),
                    "display_only": True,
                })

    require(counts == EXPECTED_COUNTS,
            f"unexpected 35th source counts: {counts}; expected {EXPECTED_COUNTS}")
    observed_keys = {(item["table"], item["id"], item["field"]) for item in candidates}
    added_spaces = sum(item["spaces_added"] for item in candidates)
    require(observed_keys == EXPECTED_CHANGE_KEYS
            and len(candidates) == len(EXPECTED_CHANGE_KEYS)
            and added_spaces == EXPECTED_ADDED_SPACES,
            f"unsafe 35th v3 drift: fields={len(candidates)}, spaces={added_spaces}; "
            f"affected={sorted(observed_keys)}")
    return {
        "kind": "35th punctuation v3 display overlay candidates",
        "exam_id": EXAM_ID,
        "punctuation_rule_version": PUNCTUATION_RULE_VERSION_V3,
        "source_backend": backend,
        "read_only": True,
        "may_apply_to_database": False,
        "display_fields_checked": len(inspected),
        "source_display_cells_sha256": _sha256(json.dumps(inspected, ensure_ascii=False, separators=(",", ":"))),
        "source_counts": counts,
        "changed_fields": len(candidates),
        "spaces_added": added_spaces,
        "changes_by_field": dict(sorted(Counter(
            f"{item['table']}.{item['field']}" for item in candidates
        ).items())),
        "candidates": candidates,
    }


def run(*, sqlite_db: Path | None = None, database_url: str | None = None,
        exam_id: str = EXAM_ID, output: Path = DEFAULT_REPORT) -> dict:
    # Complete all read-only checks before touching the report destination.
    with _readonly_store(exam_id, sqlite_db=sqlite_db, database_url=database_url) as (backend, db):
        report = scan(db, exam_id=exam_id, backend=backend)
    dest = output.resolve()
    require(dest == DEFAULT_REPORT.resolve(),
            "overlay report destination must be the fixed ignored derived JSON path")
    # A mismatched source can never overwrite a previously approved report.
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-db", type=Path, help="Explicit read-only local frozen SQLite archive")
    parser.add_argument("--exam-id", default=EXAM_ID, help="Must be 035-I-B")
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT,
                        help="Derived-only ignored JSON output")
    args = parser.parse_args(argv)
    try:
        report = run(sqlite_db=args.sqlite_db, exam_id=args.exam_id, output=args.output)
    except (AuditBlocked, RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "output": str(args.output.resolve()),
        "backend": report["source_backend"],
        "changed_fields": report["changed_fields"],
        "spaces_added": report["spaces_added"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
