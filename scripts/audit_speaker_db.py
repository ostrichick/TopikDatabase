"""Read-only, source-preserving audit of 35th/36th live transcript speaker spacing.

Use an existing verified TOPIK_DATABASE_URL. This script has no write mode and
does not update SQL, audit evidence, human decisions, or PDF source material.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.database import connect_postgres, get_database_url
from src.extraction_rules import normalize_speaker_turn_spacing


def digest(rows: list[dict]) -> str:
    data = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def audit(conn) -> dict:
    rows = [dict(row) for row in conn.execute(
        "SELECT s.exam_id,t.question_id,t.dialogue_text,t.review_status AS transcript_status,"
        "q.review_status AS question_status FROM transcripts t "
        "JOIN questions q ON q.id=t.question_id "
        "JOIN sections s ON s.id=q.section_id "
        "WHERE s.exam_id IN (%s,%s) ORDER BY s.exam_id,t.question_id",
        ("035-I-B", "036-I-B"),
    ).fetchall()]
    if len(rows) != 60 or Counter(row["exam_id"] for row in rows) != {
        "035-I-B": 30, "036-I-B": 30}:
        raise ValueError("Expected exactly 30 source-linked listening transcripts for each exam")
    report = {"mode": "read_only", "exam_sessions": {}}
    for exam_id in ("035-I-B", "036-I-B"):
        original = [row for row in rows if row["exam_id"] == exam_id]
        normalized = [{**row, "dialogue_text": normalize_speaker_turn_spacing(row["dialogue_text"])}
                      for row in original]
        changes = [row["question_id"] for row, candidate in zip(original, normalized, strict=True)
                   if row["dialogue_text"] != candidate["dialogue_text"]]
        for first in (25, 27, 29):
            a, b = (f"{exam_id[:3]}-I-L-{n:03d}" for n in (first, first + 1))
            pair = [row for row in normalized if row["question_id"] in (a, b)]
            if len(pair) != 2 or pair[0]["dialogue_text"] != pair[1]["dialogue_text"]:
                raise ValueError(f"Shared transcript pair {a}/{b} is inconsistent")
        report["exam_sessions"][exam_id] = {
            "transcripts": 30,
            "transcript_statuses": dict(Counter(row["transcript_status"] for row in original)),
            "affected_question_ids": changes,
            "affected_count": len(changes),
            "original_sha256": digest(original),
            "proposed_sha256": digest(normalized),
            "source_preserved": True,
        }
    return report


def main() -> None:
    conn = connect_postgres(get_database_url(required=True), readonly=True)
    try:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        report = audit(conn)
        conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
