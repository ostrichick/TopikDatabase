"""Shared database/config helpers for the PostgreSQL migration foundation.

The reviewer can use PostgreSQL for human-review reads/writes as of stage 6.
Stage 7 adds a small tuple-row connection surface for the append-only AI audit
runtime without changing the reviewer adapter contracts.
"""

from __future__ import annotations

import os
import sqlite3
import atexit
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEDIA_ROOT = ROOT / "topik-past-papers"
DATABASE_URL_ENV = "TOPIK_DATABASE_URL"
MEDIA_ROOT_ENV = "TOPIK_MEDIA_ROOT"


class DatabaseConfigError(RuntimeError):
    """Raised when database configuration is absent or unsafe."""


class DatabaseOperationError(RuntimeError):
    """Raised when the configured database backend cannot complete an operation."""


def get_database_url(*, required: bool = False) -> str | None:
    value = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not value:
        if required:
            raise DatabaseConfigError(f"{DATABASE_URL_ENV} is not set")
        return None
    if not (value.startswith("postgresql://") or value.startswith("postgres://")):
        raise DatabaseConfigError(
            f"{DATABASE_URL_ENV} must use a PostgreSQL connection URL"
        )
    return value


def get_media_root() -> Path:
    configured = os.environ.get(MEDIA_ROOT_ENV, "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_MEDIA_ROOT.resolve()


def _psycopg():
    try:
        import psycopg  # type: ignore
        from psycopg.rows import dict_row  # type: ignore
    except ImportError as exc:
        raise DatabaseConfigError(
            "PostgreSQL support requires a working psycopg 3/libpq installation. "
            "Install with `py -3 -m pip install \"psycopg[binary]>=3,<4\"`; "
            "if it is already installed, inspect the chained import error for an "
            "OS DLL/application-control or libpq problem."
        ) from exc
    return psycopg, dict_row


def _psycopg_tuple():
    """Return psycopg and its tuple row factory for SQLite-compatible readers."""
    try:
        import psycopg  # type: ignore
        from psycopg.rows import tuple_row  # type: ignore
    except ImportError as exc:
        raise DatabaseConfigError(
            "PostgreSQL support requires a working psycopg 3/libpq installation. "
            "Install with `py -3 -m pip install \"psycopg[binary]>=3,<4\"`; "
            "if it is already installed, inspect the chained import error for an "
            "OS DLL/application-control or libpq problem."
        ) from exc
    return psycopg, tuple_row


def connect_postgres(
    url: str | None = None,
    *,
    autocommit: bool = False,
    readonly: bool = False,
):
    """Return a standard psycopg 3 connection.

    ``readonly=True`` changes the session default before handing the connection
    to callers. Migration code deliberately leaves this false.
    """
    psycopg, dict_row = _psycopg()
    resolved = url.strip() if isinstance(url, str) and url.strip() else get_database_url(required=True)
    if not (resolved.startswith("postgresql://") or resolved.startswith("postgres://")):
        raise DatabaseConfigError("PostgreSQL connection URL is required")
    try:
        connection = psycopg.connect(resolved, autocommit=autocommit, row_factory=dict_row)
        if readonly:
            if autocommit:
                connection.execute("SET default_transaction_read_only = on")
            else:
                connection.execute("SET default_transaction_read_only = on")
                connection.commit()
        return connection
    except psycopg.Error as exc:
        raise DatabaseOperationError("PostgreSQL connection failed") from exc


@contextmanager
def postgres_connection(
    url: str | None = None,
    *,
    autocommit: bool = False,
    readonly: bool = False,
):
    """Open a standard psycopg 3 connection with mapping-like rows."""
    connection = connect_postgres(url, autocommit=autocommit, readonly=readonly)
    try:
        yield connection
    finally:
        connection.close()


def qmark_to_postgres(sql: str) -> str:
    """Translate SQLite qmark placeholders outside SQL string literals."""
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        else:
            if char in ("'", '"'):
                quote = char
                output.append(char)
            elif char == "?":
                output.append("%s")
            else:
                output.append(char)
        index += 1
    return "".join(output)


# Reviewer-only pools: the audit/migration code retains independent connections.
# Read and write sessions NEVER share a pool (and hence never share session
# read-only settings). A lease belongs to precisely one request/thread.
_review_pools: dict[tuple[str, bool], object] = {}
_review_pools_lock = threading.Lock()


def _review_pool(url: str, *, readonly: bool):
    if os.environ.get("TOPIK_REVIEW_POOL", "1").lower() in ("0", "false", "off"):
        return None
    try:
        from psycopg_pool import ConnectionPool
    except ImportError:
        # Optional package: installations without it retain the original
        # connection-per-request behavior without losing reviewer access.
        return None
    _, dict_row = _psycopg()
    key = (url, readonly)
    with _review_pools_lock:
        pool = _review_pools.get(key)
        if pool is None:
            try:
                max_size = max(1, min(4, int(os.environ.get("TOPIK_REVIEW_POOL_SIZE", "3"))))
                timeout = max(1., min(30., float(os.environ.get("TOPIK_REVIEW_POOL_TIMEOUT", "15"))))
            except ValueError as exc:
                raise DatabaseConfigError("Invalid reviewer pool size or timeout") from exc

            def configure(connection):
                # Configured only on newly opened idle sessions. A read pool
                # must never lend a writable session, even after SSH recovery.
                if readonly:
                    connection.read_only = True

            pool = ConnectionPool(url, min_size=0, max_size=max_size,
                                  timeout=timeout, kwargs={"row_factory": dict_row},
                                  configure=configure,
                                  # Validate a borrowed idle TLS/SSH socket;
                                  # psycopg_pool drops broken connections and
                                  # opens a replacement rather than lending it.
                                  check=ConnectionPool.check_connection, open=True)
            _review_pools[key] = pool
        return pool


def close_reviewer_pools():
    """Close idle reviewer connections when the local HTTP server exits."""
    with _review_pools_lock:
        pools = list(_review_pools.values())
        _review_pools.clear()
    for pool in pools:
        pool.close()


atexit.register(close_reviewer_pools)


def _reviewer_lease(url: str, *, readonly: bool, pooled: bool):
    pool = _review_pool(url, readonly=readonly) if pooled else None
    if pool is None:
        return connect_postgres(url, readonly=readonly), None
    try:
        raw = pool.getconn()
        # A returned request must have no open transaction; psycopg_pool also
        # discards broken connections, e.g. after the SSH tunnel is replaced.
        if raw.info.transaction_status.name != "IDLE" or bool(raw.read_only) != readonly:
            raw.close()
            pool.putconn(raw)
            raise DatabaseOperationError("Reviewer pool returned an unsafe session")
        return raw, pool
    except DatabaseOperationError:
        raise
    except Exception as exc:
        raise DatabaseOperationError("PostgreSQL reviewer pool unavailable") from exc


def _return_reviewer_lease(raw, pool):
    if pool is None:
        raw.close()
        return
    try:
        # On every return, including after SELECT, COMMIT, 409 or an exception.
        # Never let a subsequent request inherit snapshot/locks/failed state.
        raw.rollback()
    except Exception:
        raw.close()
    finally:
        pool.putconn(raw)  # pool discards closed/broken connections


class PostgresReadConnection:
    """Small sqlite-like read surface used by the stage-5 reviewer."""

    backend = "postgres"
    native_pg_json = True

    def __init__(self, url: str, *, pooled: bool = False):
        self._raw, self._pool = _reviewer_lease(url, readonly=True, pooled=pooled)

    def execute(self, sql: str, params=()):
        try:
            return self._raw.execute(qmark_to_postgres(sql), params)
        except Exception as exc:
            psycopg, _ = _psycopg()
            if isinstance(exc, psycopg.Error):
                raise DatabaseOperationError("PostgreSQL read failed") from exc
            raise

    def close(self):
        if self._raw is not None:
            raw, self._raw = self._raw, None
            _return_reviewer_lease(raw, self._pool)


class PostgresWriteConnection:
    """Small sqlite-like write surface used by the stage-6 reviewer.

    The connection is transactional (autocommit off). Callers must commit or
    rollback explicitly; no retry policy is implemented here.
    """

    backend = "postgres"

    def __init__(self, url: str, *, pooled: bool = False):
        self._raw, self._pool = _reviewer_lease(url, readonly=False, pooled=pooled)

    def execute(self, sql: str, params=()):
        try:
            return self._raw.execute(qmark_to_postgres(sql), params)
        except Exception as exc:
            psycopg, _ = _psycopg()
            if isinstance(exc, psycopg.Error):
                raise DatabaseOperationError("PostgreSQL write failed") from exc
            raise

    def executemany(self, sql: str, params_seq):
        try:
            with self._raw.cursor() as cursor:
                cursor.executemany(qmark_to_postgres(sql), params_seq)
        except Exception as exc:
            psycopg, _ = _psycopg()
            if isinstance(exc, psycopg.Error):
                raise DatabaseOperationError("PostgreSQL write failed") from exc
            raise

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        if self._raw is not None:
            raw, self._raw = self._raw, None
            _return_reviewer_lease(raw, self._pool)


class PostgresAuditConnection:
    """Tuple-row PostgreSQL surface used by the stage-7 AI audit runtime.

    ``ai_audit_35`` intentionally keeps its long-standing positional-row read
    contract so the SQLite implementation remains unchanged. This adapter gives
    that module the same qmark/tuple behavior on psycopg while retaining normal
    PostgreSQL transactions and an explicit no-retry policy.
    """

    backend = "postgres"
    ai_audit_tuple_rows = True

    def __init__(self, url: str, *, readonly: bool = False):
        psycopg, tuple_row = _psycopg_tuple()
        resolved = url.strip() if isinstance(url, str) and url.strip() else get_database_url(required=True)
        if not (resolved.startswith("postgresql://") or resolved.startswith("postgres://")):
            raise DatabaseConfigError("PostgreSQL connection URL is required")
        try:
            self._raw = psycopg.connect(resolved, autocommit=False, row_factory=tuple_row)
            if readonly:
                self._raw.execute("SET default_transaction_isolation = 'repeatable read'")
                self._raw.execute("SET default_transaction_read_only = on")
                self._raw.commit()
        except psycopg.Error as exc:
            raise DatabaseOperationError("PostgreSQL connection failed") from exc

    def execute(self, sql: str, params=()):
        try:
            return self._raw.execute(qmark_to_postgres(sql), params)
        except Exception as exc:
            psycopg, _ = _psycopg_tuple()
            if isinstance(exc, psycopg.Error):
                raise DatabaseOperationError("PostgreSQL AI audit operation failed") from exc
            raise

    def executemany(self, sql: str, params_seq) -> None:
        """Insert a batch on the same transaction without per-row network RTT."""
        try:
            with self._raw.cursor() as cursor:
                cursor.executemany(qmark_to_postgres(sql), params_seq)
        except Exception as exc:
            psycopg, _ = _psycopg_tuple()
            if isinstance(exc, psycopg.Error):
                raise DatabaseOperationError("PostgreSQL batch operation failed") from exc
            raise

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


@contextmanager
def sqlite_readonly(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open SQLite without permitting writes from migration/validation code."""
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite database not found: {resolved}")
    connection = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        # Pin a consistent read snapshot across multi-table migration/parity
        # queries. BEGIN is read-only under query_only and never mutates source.
        connection.execute("BEGIN")
        yield connection
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
