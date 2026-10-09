"""Fail-closed 036-I-B frozen-v5 -> v6 punctuation correction.

Default: read-only PostgreSQL inspection. --apply requires a verified local
database backup AND an independently tested restore evidence manifest. Never
run --apply before the operator has independently approved a recoverable backup.

This migration changes only 33 existing derived text cells; it preserves every
source file, raw extraction, answer, 35th source record, human review decision,
transcript status, audio segment and human review history exactly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.import_exam_staging import EXAM_ID, FROZEN_35_SHA
from scripts.migrate_36_punctuation import (
    Change, MigrationBlocked, _snapshot_35, _staging_rows, _text_rows,
    _update, digest, require,
)
from src.database import connect_postgres, get_database_url
from src.punctuation_pipeline import encoded_staging, text_fields
from src.punctuation_pipeline_v6 import (
    SOURCE_V5_SHA256, diff_report, normalize_v5, validate_for_import,
    verified_v5,
)

CORPUS = ROOT / "topik-past-papers" / "derived" / EXAM_ID
V5 = CORPUS / "staging-v5.json"
V6 = CORPUS / "staging-v6.json"
FROZEN_V6_SHA256 = "e1820e6db15f3911d32ae23d2bcaa7f401f42c0fc974b3b84d4eea3dd7759925"
EXPECTED_CHANGE_SHA256 = "c52004e8ebc94ff021b875070ab1ba995b986af3116bd1bb4051bb8124cf3c5e"
EXPECTED_FIELD_COUNTS = {"question_groups.instruction": 3, "transcripts.dialogue_text": 30}
EXPECTED_CHANGED_FIELDS = 33
EXPECTED_INSERTED_SPACES = 109
MIGRATION_KEY = "036-I-B:punctuation:v5-to-v6"


@dataclass(frozen=True)
class BackupProof:
    """Validated evidence for an operator-performed, restore-tested PG backup."""

    backup_sha256: str
    review_snapshot_sha256: str
    backup_path: str


def _hash_file(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def read_backup_proof(manifest: Path) -> BackupProof:
    """Check backup bytes and restore attestation; never create a backup here.

    Expected manifest: backup_path (absolute), backup_sha256, backed_up_at,
    restored_at (timezone-aware ISO timestamps), restore_verified=true,
    source_v5_sha256, 35th_source_sha256, review_snapshot_sha256.
    A separate disposable restore test must have been performed by the operator.
    """
    require(manifest.is_file(), "backup evidence manifest is missing")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    require(isinstance(data, dict), "backup manifest must be a JSON object")
    backup = Path(data.get("backup_path", ""))
    require(backup.is_absolute() and backup.is_file() and backup.stat().st_size > 0,
            "backup file must exist, be nonempty and have an absolute path")
    expected_hash = data.get("backup_sha256")
    require(isinstance(expected_hash, str) and len(expected_hash) == 64 and
            _hash_file(backup) == expected_hash,
            "backup file SHA-256 does not match its evidence")
    require(data.get("restore_verified") is True and data.get("restore_details"),
            "disposable restore must be independently tested and documented")
    require(data.get("source_v5_sha256") == SOURCE_V5_SHA256 and
            data.get("35th_source_sha256") == FROZEN_35_SHA,
            "backup provenance is not this 35th/36th source revision")
    try:
        backup_time = datetime.fromisoformat(data["backed_up_at"])
        restore_time = datetime.fromisoformat(data["restored_at"])
    except (KeyError, ValueError, TypeError) as exc:
        raise MigrationBlocked("backup/restore timestamps are missing or invalid") from exc
    require(backup_time.tzinfo is not None and restore_time.tzinfo is not None,
            "backup/restore timestamps must include timezones")
    now = datetime.now(timezone.utc)
    require(now - timedelta(hours=24) <= backup_time <= restore_time <= now,
            "backup or restore evidence is old, future-dated or out of sequence")
    review_hash = data.get("review_snapshot_sha256")
    require(isinstance(review_hash, str) and len(review_hash) == 64 and
            all(c in "0123456789abcdef" for c in review_hash),
            "backup review snapshot SHA-256 missing")
    return BackupProof(expected_hash, review_hash, str(backup.resolve()))


def read_frozen_staging() -> tuple[dict, dict]:
    source = verified_v5(V5)
    require(V6.is_file() and _hash_file(V6) == FROZEN_V6_SHA256,
            "v6 staging file missing or exact frozen SHA-256 differs")
    generated = normalize_v5(source)
    require(hashlib.sha256(encoded_staging(generated)).hexdigest() == FROZEN_V6_SHA256,
            "normalization no longer generates the approved v6 staging")
    target = json.loads(V6.read_text(encoding="utf-8"))
    require(target == generated, "v6 staging differs from frozen-v5 normalization")
    require(validate_for_import(source)["status"] == "validated", "v5 validator blocked")
    require(validate_for_import(target)["status"] == "validated", "v6 validator blocked")
    return source, target


def build_changes(before: dict, after: dict) -> list[Change]:
    """Exactly frozen v5 + prescribed v6 normalization, without raw/source edits."""
    require(hashlib.sha256(encoded_staging(before)).hexdigest() == SOURCE_V5_SHA256,
            "v5 must be the exact frozen baseline")
    require(hashlib.sha256(encoded_staging(after)).hexdigest() == FROZEN_V6_SHA256,
            "v6 must be the exact approved target")
    require(normalize_v5(before) == after, "v6 is not derived solely from frozen v5")
    report = diff_report(before, after)
    require(report["fields_changed"] == EXPECTED_CHANGED_FIELDS and
            report["spaces_added"] == EXPECTED_INSERTED_SPACES and
            digest(report["changes"]) == EXPECTED_CHANGE_SHA256,
            "v5-to-v6 diff signature differs from audited 33 fields / 109 spaces")
    left = list(text_fields(before))
    right = list(text_fields(after))
    require(len(left) == len(right), "v5/v6 editable text shapes differ")
    changes: list[Change] = []
    types = {"group": ("question_groups", None), "stem": ("questions", None),
             "choice": ("choices", "choice"), "transcript": ("transcripts", None)}
    for older, newer in zip(left, right, strict=True):
        path, kind, identity, node, field = older
        new_path, new_kind, new_identity, updated, new_field = newer
        require((path, kind, identity, field) == (new_path, new_kind, new_identity, new_field),
                "v6 rearranged text field identities")
        if node[field] == updated[field]:
            continue
        table = types[kind][0]
        number = None
        row_id = identity
        if kind == "choice":
            row_id, suffix = identity.rsplit("/choice-", 1)
            number = int(suffix)
        changes.append(Change(table, row_id, field, node[field], updated[field], number))
    from collections import Counter
    field_counts = dict(Counter(f"{change.table}.{change.field}" for change in changes))
    require(field_counts == EXPECTED_FIELD_COUNTS,
            f"different v6 field categories or counts: {field_counts}")
    require(len(changes) == EXPECTED_CHANGED_FIELDS and
            sum(len(c.after) - len(c.before) for c in changes) == EXPECTED_INSERTED_SPACES,
            "unexpected v6 changed cells or inserted spaces")
    return changes


def _review_snapshot(conn) -> str:
    """Capture all current human status/history, including reviewed questions.

    The 9 already approved items are permitted; no requirement for an empty
    review history or pending-only questions is imposed.
    """
    questions = [dict(r) for r in conn.execute(
        "SELECT id,review_status FROM questions WHERE section_id IN "
        "(SELECT id FROM sections WHERE exam_id=%s) ORDER BY id", (EXAM_ID,)
    ).fetchall()]
    transcripts = [dict(r) for r in conn.execute(
        "SELECT question_id,review_status FROM transcripts WHERE question_id IN "
        "(SELECT id FROM questions WHERE section_id IN "
        "(SELECT id FROM sections WHERE exam_id=%s)) ORDER BY question_id", (EXAM_ID,)
    ).fetchall()]
    audio = [dict(r) for r in conn.execute(
        "SELECT question_id,audio_asset_id,start_ms,end_ms,status,version,"
        "source_sha256,clip_relative_path,clip_sha256,updated_at FROM audio_segments "
        "WHERE question_id IN (SELECT id FROM questions WHERE section_id IN "
        "(SELECT id FROM sections WHERE exam_id=%s)) ORDER BY question_id", (EXAM_ID,)
    ).fetchall()]
    history = [dict(r) for r in conn.execute(
        "SELECT id,subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at "
        "FROM review_records WHERE subject_id IN (SELECT id FROM questions "
        "WHERE section_id IN (SELECT id FROM sections WHERE exam_id=%s)) ORDER BY id", (EXAM_ID,)
    ).fetchall()]
    require(len(questions) == 70 and len(transcripts) == 30,
            "36th human-review inventory is incomplete")
    return digest({"questions": questions, "transcripts": transcripts,
                   "audio": audio, "history": history})


def run(before: dict, after: dict, *, apply: bool = False, url: str | None = None,
        backup: BackupProof | None = None) -> dict:
    changes = build_changes(before, after)
    if apply:
        require(isinstance(backup, BackupProof),
                "--apply requires a hash-verified, restored backup proof")
    pre_rows, post_rows = _staging_rows(before), _staging_rows(after)
    metadata = {
        "version": "036-I-B-punctuation-v5-to-v6",
        "v5_sha256": SOURCE_V5_SHA256,
        "v6_sha256": FROZEN_V6_SHA256,
        "changes_sha256": EXPECTED_CHANGE_SHA256,
        "changed_cells": EXPECTED_CHANGED_FIELDS,
        "inserted_spaces": EXPECTED_INSERTED_SPACES,
    }
    marker = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    conn = connect_postgres(url or get_database_url(required=True), readonly=not apply)
    try:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        if apply:
            # Block concurrent question review, audio edits, and migrations until
            # all original/after hashes and review snapshots are rechecked.
            conn.execute(
                "LOCK TABLE exams,source_files,sections,question_groups,questions,"
                "choices,answers,images,question_images,transcripts,audio_assets,"
                "audio_segments,review_records,import_metadata IN SHARE ROW EXCLUSIVE MODE"
            )
        require(conn.execute(
            "SELECT id FROM exams WHERE id=%s AND session=36 AND level='I' AND booklet='B'",
            (EXAM_ID,),
        ).fetchone() is not None, "36th exam is missing")
        frozen_35 = _snapshot_35(conn)
        require(frozen_35 == FROZEN_35_SHA, "35th frozen source SHA-256 differs")
        review_before = _review_snapshot(conn)
        existing = conn.execute(
            "SELECT value FROM import_metadata WHERE key=%s" + (" FOR UPDATE" if apply else ""),
            (MIGRATION_KEY,),
        ).fetchone()
        actual = _text_rows(conn, lock=apply)
        require(set(actual) == set(pre_rows),
                "36th source text field keys/row mapping differ from frozen v5")
        if existing is None:
            require(actual == pre_rows,
                    "36th live text differs from all 502 frozen v5 fields; manual text edits or partial migration")
            state = "pending"
        else:
            require(existing["value"] == marker,
                    "existing v6 marker evidence differs from this exact frozen v5/v6 pair")
            require(actual == post_rows,
                    "v6 marker exists, but live 36th text differs from frozen v6")
            state = "already_applied"
        if apply and state == "pending":
            require(backup.review_snapshot_sha256 == review_before,
                    "human reviews have changed since the verified backup; take new backup")
            for change in changes:
                _update(conn, change)  # Per-cell value CAS; rowcount must be one.
            conn.execute("INSERT INTO import_metadata(key,value) VALUES(%s,%s)",
                         (MIGRATION_KEY, marker))
            require(_text_rows(conn, lock=False) == post_rows,
                    "post-write 36th text differs from exact frozen v6")
            require(_review_snapshot(conn) == review_before,
                    "human review status, audio or history changed during migration")
            require(_snapshot_35(conn) == frozen_35,
                    "35th frozen source snapshot changed during migration")
            conn.commit()
            state = "applied"
        else:
            conn.rollback()
        return {
            "status": state, "mode": "apply" if apply else "dry_run",
            "exam_id": EXAM_ID, "expected_db_fields": len(pre_rows),
            "changed_cells": len(changes), "inserted_spaces": EXPECTED_INSERTED_SPACES,
            "35th_source_sha256": frozen_35,
            "36th_review_snapshot_sha256": review_before,
            "migration_marker_key": MIGRATION_KEY, "migration": metadata,
            "changes": [c.evidence() for c in changes],
        }
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Make the 33 corrections only after independently verifying backup and restore")
    parser.add_argument("--backup-manifest", type=Path,
                        help="Mandatory --apply backup/restore evidence JSON with matching SHA-256")
    args = parser.parse_args(argv)
    try:
        before, after = read_frozen_staging()
        build_changes(before, after)
        if args.apply:
            require(args.backup_manifest is not None,
                    "--apply requires --backup-manifest and a prior verified disposable restore")
            backup = read_backup_proof(args.backup_manifest)
        else:
            backup = None
        print(json.dumps(run(before, after, apply=args.apply, backup=backup),
                         ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (MigrationBlocked, ValueError, KeyError, OSError, RuntimeError) as exc:
        print(f"36th v6 migration blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
