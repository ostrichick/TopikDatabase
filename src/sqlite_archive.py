"""Stage 10 guardrails for the preserved legacy SQLite database.

The central PostgreSQL database is the operational source of truth after the
Stage 9 cutover.  The canonical 35th TOPIK I SQLite database remains in place
only as an immutable rollback/reference artifact.  Explicit temporary SQLite
fixtures are still supported for tests and offline legacy tooling, but project
code must never reopen the canonical artifact for mutation.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SQLITE = (ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite").resolve()


class FrozenSqliteError(RuntimeError):
    """Raised when code attempts to mutate the Stage 10 SQLite archive."""


def is_canonical_sqlite(path: str | Path) -> bool:
    resolved = Path(path).expanduser().resolve()
    if resolved == CANONICAL_SQLITE:
        return True
    try:
        return resolved.exists() and CANONICAL_SQLITE.exists() and os.path.samefile(resolved, CANONICAL_SQLITE)
    except OSError:
        return False


def assert_sqlite_write_allowed(path: str | Path) -> Path:
    """Return a resolved legacy target unless it is the frozen canonical DB."""
    resolved = Path(path).expanduser().resolve()
    if is_canonical_sqlite(resolved):
        raise FrozenSqliteError(
            "Stage 10 froze 035-I-B.sqlite as an immutable rollback archive; "
            "operational writes must use central PostgreSQL"
        )
    return resolved


def assert_sqlite_connection_write_allowed(connection: sqlite3.Connection) -> None:
    """Reject a raw SQLite connection if any attached DB is the frozen archive."""
    for _sequence, _name, filename in connection.execute("PRAGMA database_list"):
        if filename and is_canonical_sqlite(filename):
            raise FrozenSqliteError(
                "Stage 10 froze 035-I-B.sqlite as an immutable rollback archive; "
                "an already-open SQLite connection cannot bypass the write guard"
            )
