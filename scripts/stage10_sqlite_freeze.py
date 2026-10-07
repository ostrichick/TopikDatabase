"""Archive and verify the Stage 10 legacy SQLite rollback artifacts.

This tool never changes SQLite database bytes.  ``freeze`` first validates the
approved Stage 9 source and the Stage 9 operational PostgreSQL configuration,
copies the canonical SQLite database plus its historical ``before-*.sqlite``
snapshots into the external runtime archive, verifies byte-for-byte SHA-256 and
SQLite integrity, and finally marks both source and archive copies read-only.

``verify`` is non-mutating and can be used after restarts or on a new device.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DERIVED = ROOT / "topik-past-papers" / "derived"
CANONICAL = DERIVED / "035-I-B.sqlite"
EXPECTED_SQLITE_SHA256 = "076826708a93718faa28c3c1ae3e87bbf28aac4bcfaed88359fd2a12b1c0b7e6"
DEFAULT_RUNTIME = ROOT.parent / "TopikDatabase-runtime"
ARCHIVE_DIR_NAME = "sqlite-archive"
MANIFEST_NAME = "stage10-manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_integrity(path: Path) -> str:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        connection.execute("PRAGMA query_only=ON")
        return connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()


def _candidate_files() -> list[Path]:
    files = [CANONICAL]
    files.extend(sorted(DERIVED.glob("035-I-B.before-*.sqlite"), key=lambda item: item.name))
    return files


def _assert_no_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            raise RuntimeError(f"SQLite sidecar must be reconciled before freeze: {sidecar.name}")


def _record(path: Path, *, archived_name: str | None = None) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    _assert_no_sqlite_sidecars(path)
    integrity = _sqlite_integrity(path)
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity_check failed for {path.name}: {integrity}")
    return {
        "name": path.name,
        "relative_path": path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path),
        "archived_name": archived_name or path.name,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "integrity_check": integrity,
    }


def _is_readonly(path: Path) -> bool:
    return not bool(path.stat().st_mode & stat.S_IWRITE)


def _set_readonly(path: Path) -> None:
    os.chmod(path, stat.S_IREAD)
    if not _is_readonly(path):
        raise RuntimeError(f"Failed to mark SQLite artifact read-only: {path}")


def _load_operational(runtime: Path) -> dict:
    config_path = runtime / "operational.json"
    if not config_path.is_file():
        raise RuntimeError("Stage 9 operational.json is missing")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    url = str(config.get("database_url", "")).strip()
    media_root = str(config.get("media_root", "")).strip()
    if not url.startswith("postgresql://"):
        raise RuntimeError("Stage 10 requires the Stage 9 PostgreSQL operational configuration")
    if "/topik?" not in url:
        raise RuntimeError("Stage 10 requires the operational topik database")
    if "sslmode=verify-full" not in url:
        raise RuntimeError("Stage 10 requires verify-full PostgreSQL TLS")
    if not media_root:
        raise RuntimeError("Stage 10 requires TOPIK_MEDIA_ROOT in operational configuration")
    return {"database": "topik", "media_root": str(Path(media_root).expanduser().resolve())}


def _verify_approved_source() -> None:
    if _sha256(CANONICAL) != EXPECTED_SQLITE_SHA256:
        raise RuntimeError("Canonical SQLite SHA-256 differs from the approved Stage 9 source")
    connection = sqlite3.connect(CANONICAL.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT relative_path,sha256,byte_size FROM source_files ORDER BY relative_path"
        ).fetchall()
    finally:
        connection.close()
    for relative, expected_sha, expected_size in rows:
        source = (ROOT / relative).resolve()
        if not source.is_file() or source.stat().st_size != expected_size or _sha256(source) != expected_sha:
            raise RuntimeError(f"Immutable source media mismatch: {relative}")


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".stage10-", suffix=".json.part", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def freeze(runtime: Path = DEFAULT_RUNTIME) -> dict:
    runtime = Path(runtime).expanduser().resolve()
    operational = _load_operational(runtime)
    _verify_approved_source()
    source_files = _candidate_files()
    source_records = [_record(path) for path in source_files]

    archive_root = runtime / ARCHIVE_DIR_NAME
    files_root = archive_root / "files"
    files_root.mkdir(parents=True, exist_ok=True)
    archived_records = []
    for source, source_record in zip(source_files, source_records):
        target = files_root / source.name
        if target.exists():
            if not target.is_file() or _sha256(target) != source_record["sha256"]:
                raise RuntimeError(f"Existing Stage 10 archive conflicts with source: {target.name}")
        else:
            shutil.copyfile(source, target)
        archive_record = _record(target, archived_name=target.name)
        if archive_record["sha256"] != source_record["sha256"] or archive_record["size_bytes"] != source_record["size_bytes"]:
            raise RuntimeError(f"Stage 10 archive copy differs from source: {source.name}")
        archived_records.append(archive_record)

    manifest = {
        "contract_version": 1,
        "stage": 10,
        "status": "frozen",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "operational_database": operational["database"],
        "media_root": operational["media_root"],
        "canonical_sqlite_sha256": EXPECTED_SQLITE_SHA256,
        "source_files": source_records,
        "archive_files": archived_records,
        "policy": {
            "operational_backend": "postgresql",
            "sqlite_operational_writes": False,
            "sqlite_implicit_fallback": False,
            "sqlite_delete_or_replace": False,
        },
    }
    manifest_path = archive_root / MANIFEST_NAME

    for path in source_files:
        _set_readonly(path)
    for path in files_root.glob("*.sqlite"):
        _set_readonly(path)
    for path in [*source_files, *files_root.glob("*.sqlite")]:
        if not _is_readonly(path):
            raise RuntimeError(f"Stage 10 freeze did not make artifact read-only: {path}")

    # Publish status=frozen only after every source/copy passed the final
    # read-only check. A partial first run therefore cannot leave a manifest
    # that falsely claims completion.
    _atomic_json(manifest_path, manifest)

    return verify(runtime)


def verify(runtime: Path = DEFAULT_RUNTIME) -> dict:
    runtime = Path(runtime).expanduser().resolve()
    operational = _load_operational(runtime)
    _verify_approved_source()
    manifest_path = runtime / ARCHIVE_DIR_NAME / MANIFEST_NAME
    if not manifest_path.is_file():
        raise RuntimeError("Stage 10 manifest is missing; run freeze first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("stage") != 10 or manifest.get("status") != "frozen":
        raise RuntimeError("Stage 10 manifest has an invalid contract/status")
    if manifest.get("canonical_sqlite_sha256") != EXPECTED_SQLITE_SHA256:
        raise RuntimeError("Stage 10 manifest canonical hash mismatch")
    if manifest.get("operational_database") != operational["database"]:
        raise RuntimeError("Stage 10 manifest operational database mismatch")

    expected_sources = {item["name"]: item for item in manifest.get("source_files", [])}
    expected_archives = {item["name"]: item for item in manifest.get("archive_files", [])}
    current_sources = _candidate_files()
    if set(expected_sources) != {path.name for path in current_sources}:
        raise RuntimeError("Stage 10 source archive set changed after freeze")
    files_root = runtime / ARCHIVE_DIR_NAME / "files"
    for source in current_sources:
        expected = expected_sources[source.name]
        current = _record(source)
        if current["sha256"] != expected["sha256"] or current["size_bytes"] != expected["size_bytes"]:
            raise RuntimeError(f"Frozen SQLite source changed: {source.name}")
        if not _is_readonly(source):
            raise RuntimeError(f"Frozen SQLite source is writable: {source.name}")
        archived = files_root / expected["archived_name"]
        if source.name not in expected_archives or not archived.is_file():
            raise RuntimeError(f"Stage 10 archive copy missing: {source.name}")
        archived_record = _record(archived)
        if archived_record["sha256"] != expected["sha256"] or archived_record["size_bytes"] != expected["size_bytes"]:
            raise RuntimeError(f"Stage 10 archive copy changed: {source.name}")
        if not _is_readonly(archived):
            raise RuntimeError(f"Stage 10 archive copy is writable: {source.name}")

    return {
        "status": "ok",
        "stage": 10,
        "operational_database": "topik",
        "sqlite_operational_writes": False,
        "canonical_sqlite_sha256": EXPECTED_SQLITE_SHA256,
        "frozen_source_files": len(current_sources),
        "archive_copies": len(current_sources),
        "manifest": str(manifest_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "verify"))
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    args = parser.parse_args(argv)
    try:
        result = freeze(args.runtime) if args.command == "freeze" else verify(args.runtime)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Stage 10 SQLite freeze blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
