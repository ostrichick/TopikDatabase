"""Structural contract checks for the additive PostgreSQL foundation."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SQLITE_SCHEMA = ROOT / "db" / "schema.sql"
POSTGRES_SCHEMA = ROOT / "db" / "schema_postgres.sql"

AUDIT_TABLES = (
    "ai_audit_source_snapshots",
    "ai_audit_runs",
    "ai_audit_passes",
    "ai_audit_checkpoints",
    "ai_audit_results",
    "ai_audit_attempts",
    "ai_audit_findings",
    "ai_audit_finding_occurrences",
)


def _append_only_function_definition_is_valid(function_def: object) -> bool:
    normalized = "".join(str(function_def).lower().split())
    return (
        "raiseexception'aiaudittablesareappend-only'" in normalized
        and "errcode='55000'" in normalized
    )


def _sqlite_contract() -> dict[str, dict[str, Any]]:
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SQLITE_SCHEMA.read_text(encoding="utf-8"))
        result = {}
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            info = db.execute(f'PRAGMA table_info("{table}")').fetchall()
            pk_columns = tuple(
                row[1] for row in sorted((row for row in info if row[5]), key=lambda row: row[5])
            )
            foreign_keys = sorted(
                (row[3], row[2], row[4], row[5], row[6])
                for row in db.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            )
            unique_sets = []
            explicit_indexes = {}
            for index in db.execute(f'PRAGMA index_list("{table}")').fetchall():
                # origin='u' is a UNIQUE table constraint; PK and explicit
                # CREATE INDEX entries are checked separately.
                if index[2] and len(index) >= 4 and index[3] == "u":
                    cols = tuple(
                        row[2]
                        for row in sorted(
                            db.execute(f'PRAGMA index_info("{index[1]}")').fetchall(),
                            key=lambda row: row[0],
                        )
                    )
                    unique_sets.append(cols)
                if len(index) >= 4 and index[3] == "c":
                    cols = tuple(
                        row[2]
                        for row in sorted(
                            db.execute(f'PRAGMA index_info("{index[1]}")').fetchall(),
                            key=lambda row: row[0],
                        )
                    )
                    explicit_indexes[index[1]] = cols
            create_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            result[table] = {
                "pk": pk_columns,
                "foreign_keys": foreign_keys,
                "unique": sorted(unique_sets),
                "check_count": len(re.findall(r"\bCHECK\s*\(", create_sql, flags=re.I)),
                "indexes": explicit_indexes,
            }
        return result
    finally:
        db.close()


def _postgres_expected_columns() -> dict[str, dict[str, dict[str, Any]]]:
    text = POSTGRES_SCHEMA.read_text(encoding="utf-8")
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);",
        text,
        flags=re.I | re.S,
    ):
        table, body = match.group(1), match.group(2)
        columns = {}
        for raw in body.splitlines():
            line = raw.strip().rstrip(",")
            column = re.match(
                r"^(\w+)\s+(DOUBLE PRECISION|BIGINT|INTEGER|TEXT|BYTEA)\b(.*)$",
                line,
                flags=re.I,
            )
            if not column:
                continue
            name, data_type, rest = column.group(1), column.group(2).lower(), column.group(3)
            default = None
            default_match = re.search(r"\bDEFAULT\s+('(?:[^']|'')*'|[^\s,]+)", rest, flags=re.I)
            if default_match:
                default = default_match.group(1)
            columns[name] = {
                "data_type": data_type,
                "not_null": bool(re.search(r"\bNOT\s+NULL\b", rest, flags=re.I)),
                "identity": bool(re.search(r"GENERATED\s+BY\s+DEFAULT\s+AS\s+IDENTITY", rest, flags=re.I)),
                "default": default,
            }
        tables[table] = columns

    sqlite_contract = _sqlite_contract()
    for table, contract in sqlite_contract.items():
        for column in contract["pk"]:
            if table in tables and column in tables[table]:
                tables[table][column]["not_null"] = True
    return tables


def _normalize_pg_default(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"::(?:text|character varying)$", "", value.strip())
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1]
    return normalized


def validate_postgres_structure(pg, expected_tables: tuple[str, ...]) -> list[dict]:
    """Return structural diffs for tables created by schema_postgres.sql."""
    diffs: list[dict] = []
    expected_columns = _postgres_expected_columns()
    sqlite_contract = _sqlite_contract()
    table_set = set(expected_tables)

    current_schema = pg.execute("SELECT current_schema() AS schema").fetchone()["schema"]
    actual_tables = {
        row["table_name"]
        for row in pg.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type='BASE TABLE'",
            (current_schema,),
        ).fetchall()
        if row["table_name"] in table_set
    }
    missing_tables = sorted(table_set - actual_tables)
    if missing_tables:
        diffs.append({"problem": "missing_tables", "tables": missing_tables})

    rows = pg.execute(
        "SELECT table_name,column_name,data_type,is_nullable,column_default,is_identity,identity_generation "
        "FROM information_schema.columns WHERE table_schema=%s ORDER BY table_name,ordinal_position",
        (current_schema,),
    ).fetchall()
    actual_columns: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        if row["table_name"] in table_set:
            actual_columns[row["table_name"]][row["column_name"]] = dict(row)

    for table in expected_tables:
        expected = expected_columns.get(table, {})
        actual = actual_columns.get(table, {})
        if list(expected) != list(actual):
            diffs.append({
                "table": table,
                "problem": "column_structure_mismatch",
                "expected": list(expected),
                "actual": list(actual),
            })
            continue
        for column, contract in expected.items():
            value = actual[column]
            actual_type = value["data_type"].lower()
            if actual_type != contract["data_type"]:
                diffs.append({
                    "table": table, "column": column, "problem": "type_mismatch",
                    "expected": contract["data_type"], "actual": actual_type,
                })
            actual_not_null = value["is_nullable"] == "NO"
            if actual_not_null != contract["not_null"]:
                diffs.append({
                    "table": table, "column": column, "problem": "nullability_mismatch",
                    "expected_not_null": contract["not_null"], "actual_not_null": actual_not_null,
                })
            actual_identity = value["is_identity"] == "YES"
            if actual_identity != contract["identity"]:
                diffs.append({
                    "table": table, "column": column, "problem": "identity_mismatch",
                    "expected": contract["identity"], "actual": actual_identity,
                })
            if contract["identity"] and value["identity_generation"] != "BY DEFAULT":
                diffs.append({
                    "table": table, "column": column, "problem": "identity_generation_mismatch",
                    "expected": "BY DEFAULT", "actual": value["identity_generation"],
                })
            if not contract["identity"]:
                actual_default = _normalize_pg_default(value["column_default"])
                if actual_default != contract["default"]:
                    diffs.append({
                        "table": table, "column": column, "problem": "default_mismatch",
                        "expected": contract["default"], "actual": actual_default,
                    })

    # PK/UNIQUE column sets.
    key_rows = pg.execute(
        "SELECT cl.relname AS table_name,con.conname,con.contype,att.attname AS column_name,u.ord "
        "FROM pg_constraint con "
        "JOIN pg_class cl ON cl.oid=con.conrelid "
        "JOIN pg_namespace ns ON ns.oid=cl.relnamespace "
        "JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS u(attnum,ord) ON true "
        "JOIN pg_attribute att ON att.attrelid=cl.oid AND att.attnum=u.attnum "
        "WHERE ns.nspname=%s AND con.contype IN ('p','u') "
        "ORDER BY cl.relname,con.conname,u.ord",
        (current_schema,),
    ).fetchall()
    grouped: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for row in key_rows:
        if row["table_name"] in table_set:
            grouped[(row["table_name"], row["contype"], row["conname"])].append(row["column_name"])
    for table in expected_tables:
        expected = sqlite_contract.get(table)
        if expected is None:
            continue
        actual_pk = [tuple(cols) for (t, kind, _), cols in grouped.items() if t == table and kind == "p"]
        actual_unique = sorted(
            tuple(cols) for (t, kind, _), cols in grouped.items() if t == table and kind == "u"
        )
        if actual_pk != [expected["pk"]]:
            diffs.append({"table": table, "problem": "primary_key_mismatch", "actual": actual_pk})
        if actual_unique != expected["unique"]:
            diffs.append({
                "table": table, "problem": "unique_constraint_mismatch",
                "expected": expected["unique"], "actual": actual_unique,
            })

    # FK source/ref column triples.
    fk_rows = pg.execute(
        "SELECT srccl.relname AS table_name,srcatt.attname AS source_column,"
        "refcl.relname AS ref_table,refatt.attname AS ref_column,refns.nspname AS ref_schema,"
        "con.confupdtype AS update_action,con.confdeltype AS delete_action "
        "FROM pg_constraint con "
        "JOIN pg_class srccl ON srccl.oid=con.conrelid "
        "JOIN pg_namespace srcns ON srcns.oid=srccl.relnamespace "
        "JOIN pg_class refcl ON refcl.oid=con.confrelid "
        "JOIN pg_namespace refns ON refns.oid=refcl.relnamespace "
        "JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS src(attnum,ord) ON true "
        "JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS ref(attnum,ord) ON ref.ord=src.ord "
        "JOIN pg_attribute srcatt ON srcatt.attrelid=srccl.oid AND srcatt.attnum=src.attnum "
        "JOIN pg_attribute refatt ON refatt.attrelid=refcl.oid AND refatt.attnum=ref.attnum "
        "WHERE srcns.nspname=%s AND con.contype='f'",
        (current_schema,),
    ).fetchall()
    actual_fks: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    action_name = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
    for row in fk_rows:
        if row["table_name"] in table_set:
            if row["ref_schema"] != current_schema:
                diffs.append({
                    "table": row["table_name"], "problem": "foreign_key_schema_mismatch",
                    "ref_table": row["ref_table"], "expected_schema": current_schema,
                    "actual_schema": row["ref_schema"],
                })
            actual_fks[row["table_name"]].append(
                (
                    row["source_column"], row["ref_table"], row["ref_column"],
                    action_name.get(row["update_action"], row["update_action"]),
                    action_name.get(row["delete_action"], row["delete_action"]),
                )
            )
    for table in expected_tables:
        expected = sqlite_contract.get(table)
        if expected is not None and sorted(actual_fks[table]) != expected["foreign_keys"]:
            diffs.append({
                "table": table, "problem": "foreign_key_mismatch",
                "expected": expected["foreign_keys"], "actual": sorted(actual_fks[table]),
            })

    check_rows = pg.execute(
        "SELECT cl.relname AS table_name,COUNT(*) AS count FROM pg_constraint con "
        "JOIN pg_class cl ON cl.oid=con.conrelid JOIN pg_namespace ns ON ns.oid=cl.relnamespace "
        "WHERE ns.nspname=%s AND con.contype='c' GROUP BY cl.relname",
        (current_schema,),
    ).fetchall()
    actual_checks = {row["table_name"]: row["count"] for row in check_rows}
    for table in expected_tables:
        expected = sqlite_contract.get(table)
        if expected is not None and actual_checks.get(table, 0) != expected["check_count"]:
            diffs.append({
                "table": table, "problem": "check_constraint_count_mismatch",
                "expected": expected["check_count"], "actual": actual_checks.get(table, 0),
            })

    expected_indexes = {
        name: (table, columns)
        for table, contract in sqlite_contract.items()
        for name, columns in contract["indexes"].items()
    }
    index_rows = pg.execute(
        "SELECT tbl.relname AS table_name,idx.relname AS index_name,att.attname AS column_name,k.ord "
        "FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid "
        "JOIN pg_class tbl ON tbl.oid=i.indrelid JOIN pg_namespace ns ON ns.oid=tbl.relnamespace "
        "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum,ord) ON true "
        "JOIN pg_attribute att ON att.attrelid=tbl.oid AND att.attnum=k.attnum "
        "WHERE ns.nspname=%s ORDER BY tbl.relname,idx.relname,k.ord",
        (current_schema,),
    ).fetchall()
    actual_index_columns: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in index_rows:
        actual_index_columns[(row["table_name"], row["index_name"])].append(row["column_name"])
    for name, (table, expected_columns_for_index) in expected_indexes.items():
        actual_columns_for_index = tuple(actual_index_columns.get((table, name), []))
        if actual_columns_for_index != expected_columns_for_index:
            diffs.append({
                "table": table,
                "problem": "explicit_index_mismatch",
                "index": name,
                "expected": expected_columns_for_index,
                "actual": actual_columns_for_index,
            })

    trigger_rows = pg.execute(
        "SELECT cl.relname AS table_name,t.tgname,t.tgenabled,t.tgtype,p.proname,fn.nspname AS function_schema,"
        "pg_get_functiondef(p.oid) AS function_def "
        "FROM pg_trigger t JOIN pg_class cl ON cl.oid=t.tgrelid "
        "JOIN pg_namespace ns ON ns.oid=cl.relnamespace "
        "JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace fn ON fn.oid=p.pronamespace "
        "WHERE ns.nspname=%s AND NOT t.tgisinternal",
        (current_schema,),
    ).fetchall()
    by_table = defaultdict(list)
    for row in trigger_rows:
        by_table[row["table_name"]].append(row)
    for table in AUDIT_TABLES:
        expected_name = f"{table}_append_only"
        matches = [row for row in by_table[table] if row["tgname"] == expected_name]
        valid = False
        if len(matches) == 1:
            row = matches[0]
            tgtype = row["tgtype"]
            valid = (
                row["tgenabled"] in ("O", "A")
                and row["proname"] == "topik_reject_ai_audit_mutation"
                and row["function_schema"] == current_schema
                and _append_only_function_definition_is_valid(row["function_def"])
                and bool(tgtype & 1)       # ROW
                and bool(tgtype & 2)       # BEFORE
                and not bool(tgtype & 4)   # INSERT
                and bool(tgtype & 8)       # DELETE
                and bool(tgtype & 16)      # UPDATE
                and not bool(tgtype & 32)  # TRUNCATE
            )
        if not valid:
            diffs.append({"table": table, "problem": "append_only_trigger_mismatch"})

    return diffs
