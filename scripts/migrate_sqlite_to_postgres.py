"""Safely migrate the 35-I pilot SQLite database into empty PostgreSQL tables.

Default mode is read-only dry-run. ``--apply`` is required for any PostgreSQL
write. The source SQLite database is always opened read-only and its SHA-256 is
checked before/after the operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.database import get_database_url, postgres_connection, sqlite_readonly
from src.postgres_contract import validate_postgres_structure


DEFAULT_SOURCE = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
POSTGRES_SCHEMA = ROOT / "db" / "schema_postgres.sql"

TABLES = (
    "exams",
    "source_files",
    "sections",
    "question_groups",
    "questions",
    "choices",
    "answers",
    "images",
    "question_images",
    "audio_assets",
    "audio_segments",
    "transcripts",
    "review_records",
    "import_metadata",
    "ai_audit_source_snapshots",
    "ai_audit_runs",
    "ai_audit_passes",
    "ai_audit_checkpoints",
    "ai_audit_results",
    "ai_audit_attempts",
    "ai_audit_findings",
    "ai_audit_finding_occurrences",
)

OPTIONAL_SOURCE_TABLES = frozenset(name for name in TABLES if name.startswith("ai_audit_"))
IDENTITY_TABLES = (
    "source_files",
    "review_records",
    "ai_audit_checkpoints",
    "ai_audit_results",
    "ai_audit_attempts",
    "ai_audit_finding_occurrences",
)

EXPECTED_BASELINE = {
    "questions": 70,
    "review_status": {"needs_manual_review": 68, "verified": 2},
    "review_records": 124,
    "candidate_audio": 30,
}


class MigrationError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sqlite_table_names(db) -> set[str]:
    return {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def inspect_source(path: Path, *, require_baseline: bool = True) -> dict:
    before_hash = file_sha256(path)
    with sqlite_readonly(path) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = len(db.execute("PRAGMA foreign_key_check").fetchall())
        tables = sqlite_table_names(db)
        missing = sorted(set(TABLES) - OPTIONAL_SOURCE_TABLES - tables)
        if missing:
            raise MigrationError(f"SQLite source is missing required tables: {missing}")

        counts = {
            table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in TABLES
            if table in tables
        }
        review_status = dict(
            db.execute(
                "SELECT review_status,COUNT(*) FROM questions GROUP BY review_status ORDER BY review_status"
            ).fetchall()
        )
        candidate_audio = db.execute(
            "SELECT COUNT(*) FROM audio_segments WHERE status='candidate'"
        ).fetchone()[0]

    after_hash = file_sha256(path)
    if before_hash != after_hash:
        raise MigrationError("SQLite source changed while being inspected")
    if integrity != "ok" or fk_violations:
        raise MigrationError(
            f"SQLite source failed integrity checks: integrity={integrity!r}, fk_violations={fk_violations}"
        )

    summary = {
        "source": str(path.resolve()),
        "source_sha256": before_hash,
        "integrity_check": integrity,
        "foreign_key_violations": fk_violations,
        "tables_present": sorted(tables & set(TABLES)),
        "tables_absent_optional": sorted(OPTIONAL_SOURCE_TABLES - tables),
        "counts": counts,
        "review_status": review_status,
        "candidate_audio": candidate_audio,
    }
    if require_baseline:
        actual = {
            "questions": counts.get("questions", 0),
            "review_status": review_status,
            "review_records": counts.get("review_records", 0),
            "candidate_audio": candidate_audio,
        }
        if actual != EXPECTED_BASELINE:
            raise MigrationError(
                "SQLite source does not match the approved 35-I baseline: "
                f"expected={EXPECTED_BASELINE!r}, actual={actual!r}"
            )
        summary["approved_baseline"] = True
    return summary


def _sqlite_columns(db, table: str) -> list[str]:
    return [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]


def _target_must_be_unused(pg) -> None:
    rows = pg.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema=current_schema() AND table_type='BASE TABLE' AND table_name=ANY(%s)",
        (list(TABLES),),
    ).fetchall()
    existing = sorted(row["table_name"] for row in rows)
    if existing:
        raise MigrationError(
            "PostgreSQL target already contains TOPIK tables; use a fresh schema/database: "
            f"{existing}"
        )


def _target_must_be_empty(pg) -> None:
    nonempty = {}
    for table in TABLES:
        count = pg.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()["count"]
        if count:
            nonempty[table] = count
    if nonempty:
        raise MigrationError(f"PostgreSQL target is not empty: {nonempty}")


def _reset_identity(pg, table: str) -> None:
    pg.execute(
        f"SELECT setval(pg_get_serial_sequence('{table}','id'), "
        f"COALESCE(MAX(id), 1), COUNT(*) > 0) FROM \"{table}\""
    )


def migrate(source: Path, *, require_baseline: bool = True) -> dict:
    source = source.resolve()
    source_summary = inspect_source(source, require_baseline=require_baseline)
    source_hash = source_summary["source_sha256"]
    schema_sql = POSTGRES_SCHEMA.read_text(encoding="utf-8")
    url = get_database_url(required=True)
    inserted: dict[str, int] = {}

    with sqlite_readonly(source) as sqlite_db, postgres_connection(url) as pg:
        try:
            # PostgreSQL DDL is transactional, so schema and all copied rows are
            # rolled back together if any insert or validation step fails.
            _target_must_be_unused(pg)
            pg.execute(schema_sql)
            structural_diffs = validate_postgres_structure(pg, TABLES)
            if structural_diffs:
                raise MigrationError(f"PostgreSQL schema contract mismatch: {structural_diffs}")
            _target_must_be_empty(pg)
            source_tables = sqlite_table_names(sqlite_db)

            for table in TABLES:
                if table not in source_tables:
                    if table in OPTIONAL_SOURCE_TABLES:
                        inserted[table] = 0
                        continue
                    raise MigrationError(f"Required source table disappeared: {table}")
                columns = _sqlite_columns(sqlite_db, table)
                rows = sqlite_db.execute(f'SELECT * FROM "{table}"').fetchall()
                inserted[table] = len(rows)
                if not rows:
                    continue
                quoted = ",".join(f'"{column}"' for column in columns)
                placeholders = ",".join(["%s"] * len(columns))
                statement = f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})'
                with pg.cursor() as cursor:
                    cursor.executemany(statement, [tuple(row) for row in rows])

            for table in IDENTITY_TABLES:
                _reset_identity(pg, table)

            # Recheck source before committing the target transaction.
            if file_sha256(source) != source_hash:
                raise MigrationError("SQLite source changed during migration; PostgreSQL rollback required")
            pg.commit()
        except BaseException:
            pg.rollback()
            raise

    return {
        "status": "applied",
        "source_sha256": source_hash,
        "inserted": inserted,
        "approved_baseline": source_summary.get("approved_baseline", False),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only SQLite to transactional PostgreSQL migration for the 35-I pilot"
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply schema and rows to the empty PostgreSQL target in TOPIK_DATABASE_URL",
    )
    parser.add_argument(
        "--skip-approved-baseline",
        action="store_true",
        help="Allow a source other than the currently approved 35-I production baseline",
    )
    args = parser.parse_args(argv)
    require_baseline = not args.skip_approved_baseline
    try:
        if args.apply:
            result = migrate(args.source, require_baseline=require_baseline)
        else:
            result = {
                "status": "dry-run",
                **inspect_source(args.source.resolve(), require_baseline=require_baseline),
                "postgresql_write_attempted": False,
            }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (MigrationError, OSError, ValueError, RuntimeError) as exc:
        print(f"Migration blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
