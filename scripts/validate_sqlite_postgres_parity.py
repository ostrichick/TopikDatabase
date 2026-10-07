"""Deep parity checks between the preserved SQLite pilot and PostgreSQL copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.database import get_database_url, postgres_connection, sqlite_readonly
from src.postgres_contract import validate_postgres_structure
from scripts.migrate_sqlite_to_postgres import (
    DEFAULT_SOURCE,
    EXPECTED_BASELINE,
    OPTIONAL_SOURCE_TABLES,
    TABLES,
    inspect_source,
    sqlite_table_names,
)


class ParityError(RuntimeError):
    pass


def _normalize(value: Any) -> Any:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return {
            "__bytes_len__": len(value),
            "__bytes_sha256__": hashlib.sha256(value).hexdigest(),
        }
    return value


def _canonical_row(columns: list[str], values) -> str:
    payload = {column: _normalize(value) for column, value in zip(columns, values)}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_rows(columns: list[str], rows) -> str:
    canonical = sorted(_canonical_row(columns, row) for row in rows)
    return hashlib.sha256("\n".join(canonical).encode("utf-8")).hexdigest()


def _sqlite_table(db, table: str) -> tuple[list[str], list, str]:
    columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
    rows = db.execute(f'SELECT * FROM "{table}"').fetchall()
    return columns, rows, _digest_rows(columns, rows)


def _postgres_columns(pg, table: str) -> list[str]:
    rows = pg.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [row["column_name"] for row in rows]


def _postgres_table(pg, table: str) -> tuple[list[str], list, str]:
    columns = _postgres_columns(pg, table)
    rows_dict = pg.execute(f'SELECT * FROM "{table}"').fetchall()
    rows = [tuple(row[column] for column in columns) for row in rows_dict]
    return columns, rows, _digest_rows(columns, rows)


def _validate_image_hashes_sqlite(db) -> list[str]:
    failures = []
    for row in db.execute("SELECT key,sha256,bytes FROM images ORDER BY key"):
        if hashlib.sha256(row[2]).hexdigest() != row[1]:
            failures.append(row[0])
    return failures


def _validate_image_hashes_postgres(pg) -> list[str]:
    failures = []
    for row in pg.execute('SELECT key,sha256,bytes FROM images ORDER BY key').fetchall():
        payload = bytes(row["bytes"])
        if hashlib.sha256(payload).hexdigest() != row["sha256"]:
            failures.append(row["key"])
    return failures


def validate_postgres(source: Path) -> dict:
    source_summary = inspect_source(source, require_baseline=True)
    url = get_database_url(required=True)
    diffs: list[dict] = []
    table_results = {}

    with sqlite_readonly(source) as sqlite_db, postgres_connection(url) as pg:
        structural_diffs = validate_postgres_structure(pg, TABLES)
        if structural_diffs:
            diffs.extend(structural_diffs)
        source_tables = sqlite_table_names(sqlite_db)
        for table in TABLES:
            pg_columns, pg_rows, pg_digest = _postgres_table(pg, table)
            if table not in source_tables:
                if table not in OPTIONAL_SOURCE_TABLES:
                    diffs.append({"table": table, "problem": "missing_source_table"})
                elif pg_rows:
                    diffs.append({"table": table, "problem": "target_not_empty_for_absent_optional_source"})
                table_results[table] = {
                    "source_count": 0,
                    "postgres_count": len(pg_rows),
                    "source_absent_optional": table in OPTIONAL_SOURCE_TABLES,
                }
                continue

            sqlite_columns, sqlite_rows, sqlite_digest = _sqlite_table(sqlite_db, table)
            if sqlite_columns != pg_columns:
                diffs.append({
                    "table": table,
                    "problem": "column_mismatch",
                    "sqlite": sqlite_columns,
                    "postgres": pg_columns,
                })
            if len(sqlite_rows) != len(pg_rows) or sqlite_digest != pg_digest:
                diffs.append({
                    "table": table,
                    "problem": "content_mismatch",
                    "sqlite_count": len(sqlite_rows),
                    "postgres_count": len(pg_rows),
                    "sqlite_digest": sqlite_digest,
                    "postgres_digest": pg_digest,
                })
            table_results[table] = {
                "source_count": len(sqlite_rows),
                "postgres_count": len(pg_rows),
                "source_digest": sqlite_digest,
                "postgres_digest": pg_digest,
            }

        sqlite_image_failures = _validate_image_hashes_sqlite(sqlite_db)
        pg_image_failures = _validate_image_hashes_postgres(pg)
        if sqlite_image_failures or pg_image_failures:
            diffs.append({
                "problem": "image_blob_sha256_mismatch",
                "sqlite": sqlite_image_failures,
                "postgres": pg_image_failures,
            })

        # Target semantic baseline independently verifies the rows most important
        # to resuming human review on another device.
        pg_question_count = pg.execute("SELECT COUNT(*) AS count FROM questions").fetchone()["count"]
        pg_review_status = {
            row["review_status"]: row["count"]
            for row in pg.execute(
                "SELECT review_status,COUNT(*) AS count FROM questions GROUP BY review_status ORDER BY review_status"
            ).fetchall()
        }
        pg_review_records = pg.execute("SELECT COUNT(*) AS count FROM review_records").fetchone()["count"]
        pg_candidate_audio = pg.execute(
            "SELECT COUNT(*) AS count FROM audio_segments WHERE status='candidate'"
        ).fetchone()["count"]
        target_baseline = {
            "questions": pg_question_count,
            "review_status": pg_review_status,
            "review_records": pg_review_records,
            "candidate_audio": pg_candidate_audio,
        }
        if target_baseline != EXPECTED_BASELINE:
            diffs.append({
                "problem": "target_baseline_mismatch",
                "expected": EXPECTED_BASELINE,
                "actual": target_baseline,
            })

    return {
        "status": "ok" if not diffs else "diff",
        "diff_count": len(diffs),
        "diffs": diffs,
        "source": source_summary,
        "target_baseline": target_baseline,
        "tables": table_results,
        "postgres_structure_diff_count": len(structural_diffs),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate SQLite/PostgreSQL 35-I migration parity")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--source-only",
        action="store_true",
        help="Validate the SQLite baseline deeply without requiring PostgreSQL",
    )
    args = parser.parse_args(argv)
    try:
        source = args.source.resolve()
        if args.source_only:
            summary = inspect_source(source, require_baseline=True)
            with sqlite_readonly(source) as db:
                source_tables = sqlite_table_names(db)
                digests = {}
                for table in TABLES:
                    if table in source_tables:
                        columns, rows, digest = _sqlite_table(db, table)
                        digests[table] = {"columns": columns, "count": len(rows), "digest": digest}
                bad_images = _validate_image_hashes_sqlite(db)
            result = {
                "status": "source-ok" if not bad_images else "source-invalid",
                "source": summary,
                "table_digests": digests,
                "image_blob_sha256_failures": bad_images,
                "postgresql_compared": False,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if not bad_images else 1

        result = validate_postgres(source)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["diff_count"] == 0 else 1
    except (ParityError, OSError, ValueError, RuntimeError) as exc:
        print(f"Parity validation blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
