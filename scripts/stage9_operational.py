"""Stage 9 operational PostgreSQL helper.

This helper deliberately keeps credentials out of command arguments and stdout.
Secrets live outside the repository under the caller-supplied operational base.
It never opens the source SQLite database writable and never modifies source
PDF/MP3 media.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
EXPECTED_SQLITE_SHA256 = "076826708a93718faa28c3c1ae3e87bbf28aac4bcfaed88359fd2a12b1c0b7e6"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_immutable_source() -> dict:
    sqlite_sha = _sha256(SOURCE_DB)
    if sqlite_sha != EXPECTED_SQLITE_SHA256:
        raise RuntimeError("immutable SQLite SHA-256 does not match the approved Stage 9 source")
    connection = sqlite3.connect(SOURCE_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT relative_path,sha256,byte_size FROM source_files ORDER BY relative_path"
        ).fetchall()
    finally:
        connection.close()
    verified = 0
    for relative, expected_sha, expected_size in rows:
        path = (ROOT / relative).resolve()
        if not path.is_file() or path.stat().st_size != expected_size or _sha256(path) != expected_sha:
            raise RuntimeError(f"immutable source media mismatch: {relative}")
        verified += 1
    return {"sqlite_sha256": sqlite_sha, "source_media_verified": verified}


def provision_app(base: Path, *, host_ip: str, port: int, database: str) -> dict:
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise RuntimeError("psycopg 3 is required for Stage 9 provisioning") from exc

    secrets_dir = base / "secrets"
    admin_path = secrets_dir / "admin.pw"
    app_path = secrets_dir / "app.pw"
    admin_password = admin_path.read_text(encoding="utf-8").strip()
    app_password = app_path.read_text(encoding="utf-8").strip()
    if not admin_password or not app_password:
        raise RuntimeError("Stage 9 credential files are empty")

    admin_url = f"postgresql://topik_admin@127.0.0.1:{port}/postgres"
    connection = psycopg.connect(admin_url, password=admin_password, autocommit=True)
    try:
        role = connection.execute("SELECT 1 FROM pg_roles WHERE rolname='topik_app'").fetchone()
        if role is None:
            connection.execute(
                sql.SQL(
                    "CREATE ROLE topik_app LOGIN PASSWORD {} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
                ).format(sql.Literal(app_password))
            )
        db = connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone()
        if db is None:
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER topik_app").format(sql.Identifier(database))
            )
    finally:
        connection.close()

    pgpass = secrets_dir / "app-pgpass.conf"
    pgpass.write_text(
        f"127.0.0.1:{port}:*:topik_app:{app_password}\n"
        f"{host_ip}:{port}:*:topik_app:{app_password}\n",
        encoding="ascii",
    )
    try:
        os.chmod(pgpass, 0o600)
    except OSError:
        pass
    return {
        "status": "ready",
        "database": database,
        "role": "topik_app",
        "pgpass_path": str(pgpass),
    }


def reset_database(base: Path, *, port: int, database: str) -> dict:
    """Drop/recreate one Stage-9 database as topik_app using local admin auth."""
    if database not in {"topik_stage9_candidate", "topik"}:
        raise RuntimeError("Refusing to reset an unapproved Stage 9 database name")
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise RuntimeError("psycopg 3 is required for Stage 9 provisioning") from exc

    admin_password = (base / "secrets" / "admin.pw").read_text(encoding="utf-8").strip()
    if not admin_password:
        raise RuntimeError("Stage 9 admin credential file is empty")
    connection = psycopg.connect(
        f"postgresql://topik_admin@127.0.0.1:{port}/postgres",
        password=admin_password,
        autocommit=True,
    )
    try:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname=%s AND pid<>pg_backend_pid()",
            (database,),
        )
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER topik_app").format(sql.Identifier(database))
        )
    finally:
        connection.close()
    return {"status": "reset", "database": database, "owner": "topik_app"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("verify-source")

    provision = sub.add_parser("provision-app")
    provision.add_argument("--base", type=Path, required=True)
    provision.add_argument("--host-ip", required=True)
    provision.add_argument("--port", type=int, default=55432)
    provision.add_argument("--database", default="topik_stage9_candidate")

    reset = sub.add_parser("reset-database")
    reset.add_argument("--base", type=Path, required=True)
    reset.add_argument("--port", type=int, default=55432)
    reset.add_argument("--database", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "verify-source":
            result = verify_immutable_source()
        elif args.command == "provision-app":
            result = provision_app(
                args.base.expanduser().resolve(),
                host_ip=args.host_ip,
                port=args.port,
                database=args.database,
            )
        else:
            result = reset_database(
                args.base.expanduser().resolve(),
                port=args.port,
                database=args.database,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Stage 9 operational helper blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
