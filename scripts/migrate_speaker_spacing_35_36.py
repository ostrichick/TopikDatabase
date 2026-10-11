"""Apply only verified speaker-label spacing to existing 35/36 PG transcripts.

Dry-run is read-only. --apply requires a fresh SHA-verified dump and independently
restored identical preflight. Preserve review decisions/history, historical AI
audit snapshots, original PDFs/MP3s and verified question content. Source-change
markers increment all 35/36 reviewer form versions (old versions conflict).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.import_exam_staging import FROZEN_35_SHA
from scripts.migrate_36_punctuation import _snapshot_35
from src import ai_audit_35
from src.database import connect_postgres, get_database_url
from src.extraction_rules import SPEAKER_TURN_RULE_VERSION, normalize_speaker_turn_spacing

EXAMS = ("035-I-B", "036-I-B")
MARKERS = tuple(f"{exam}:transcript-speaker:v1" for exam in EXAMS)
ORIGINAL_SNAPSHOT_35 = "631c4fb71784439961956b582d0e66847eb862e2c2370f49c89d5ca520057c35"
# Audited on 2026-10-11 against an unmodified production REPEATABLE READ snapshot;
# fail closed if another reviewer has edited a source prior to migration.
EXPECTED_ROW_SHA = {
    "035-I-B": "3b9704a54d90411ec11d8ca2aaf86027694d541374dbb721e603b7671dcb64a5",
    "036-I-B": "3883a080413a1d2675ac9080fd96a937e903706c1539e8ad7ce83a10194f5d44",
}


class MigrationBlocked(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise MigrationBlocked(message)


def checksum(data) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def source_rows(conn):
    return [dict(row) for row in conn.execute(
        "SELECT s.exam_id,t.question_id,t.dialogue_text,t.review_status AS transcript_status,"
        "q.review_status AS question_status FROM transcripts t "
        "JOIN questions q ON q.id=t.question_id JOIN sections s ON s.id=q.section_id "
        "WHERE s.exam_id IN (%s,%s) ORDER BY s.exam_id,t.question_id", EXAMS,
    ).fetchall()]


def invariants(conn) -> str:
    """Snapshot human reviews, statuses, audio and all other source text/metadata."""
    scopes = {
        "questions": ("SELECT q.* FROM questions q JOIN sections s ON q.section_id=s.id "
                      "WHERE s.exam_id IN (%s,%s) ORDER BY q.id", EXAMS),
        "groups": ("SELECT g.* FROM question_groups g JOIN sections s ON g.section_id=s.id "
                   "WHERE s.exam_id IN (%s,%s) ORDER BY g.id", EXAMS),
        "choices": ("SELECT c.* FROM choices c JOIN questions q ON c.question_id=q.id "
                    "JOIN sections s ON q.section_id=s.id "
                    "WHERE s.exam_id IN (%s,%s) ORDER BY c.question_id,c.number", EXAMS),
        "answers": ("SELECT a.* FROM answers a JOIN questions q ON a.question_id=q.id "
                    "JOIN sections s ON q.section_id=s.id "
                    "WHERE s.exam_id IN (%s,%s) ORDER BY a.question_id", EXAMS),
        "review_records": ("SELECT r.* FROM review_records r JOIN questions q ON r.subject_id=q.id "
                           "JOIN sections s ON q.section_id=s.id "
                           "WHERE s.exam_id IN (%s,%s) ORDER BY r.id", EXAMS),
        "audio_segments": ("SELECT a.* FROM audio_segments a JOIN questions q ON a.question_id=q.id "
                           "JOIN sections s ON q.section_id=s.id "
                           "WHERE s.exam_id IN (%s,%s) ORDER BY a.question_id", EXAMS),
        "transcripts_excluding_text": (
            "SELECT t.question_id,t.source_file_id,t.source_pdf_page,t.review_status,t.warnings_json "
            "FROM transcripts t JOIN questions q ON t.question_id=q.id JOIN sections s "
            "ON s.id=q.section_id WHERE s.exam_id IN (%s,%s) ORDER BY t.question_id", EXAMS),
    }
    data = {name: [dict(row) for row in conn.execute(sql, params).fetchall()]
            for name, (sql, params) in scopes.items()}
    require(len(data["questions"]) == 140 and len(data["transcripts_excluding_text"]) == 60,
            "Unexpected exam inventory")
    return checksum(data)


def _row_hash(rows, exam):
    return checksum([r for r in rows if r["exam_id"] == exam])


def expected_35_source_hash(conn, normalized_35: str) -> str:
    """Compute post-revision 35 source without changing any DB source or old audit."""
    from scripts.migrate_36_punctuation import _CachedSnapshotAdapter
    payload = ai_audit_35._source_snapshot_payload(_CachedSnapshotAdapter(conn))
    changed = [q for q in payload["questions"] if q["id"] == "035-I-L-001"]
    require(len(changed) == 1 and changed[0]["transcript"] is not None,
            "35 source snapshot lacks first transcript")
    changed[0]["transcript"]["text"] = normalized_35
    updated_sha = ai_audit_35._sha(payload)
    require(updated_sha == FROZEN_35_SHA,
            "Expected 35th successor fingerprint differs from approved code contract")
    return updated_sha


def inspect(conn) -> tuple[dict, list[tuple[str, str, str]], list[dict]]:
    before = source_rows(conn)
    require(len(before) == 60 and all(sum(r["exam_id"] == exam for r in before) == 30 for exam in EXAMS),
            "Unexpected transcript row count")
    changes = [(r["question_id"], r["dialogue_text"], normalize_speaker_turn_spacing(r["dialogue_text"]))
               for r in before if normalize_speaker_turn_spacing(r["dialogue_text"]) != r["dialogue_text"]]
    records = [dict(row) for row in conn.execute(
        "SELECT key,value FROM import_metadata WHERE key IN (%s,%s) ORDER BY key", MARKERS,
    ).fetchall()]
    if not records:
        for exam in EXAMS:
            require(_row_hash(before, exam) == EXPECTED_ROW_SHA[exam],
                    f"{exam} live transcript snapshot changed; fresh independent audit required")
        require(len(changes) == 31 and sum(q.startswith("035-") for q, *_ in changes) == 1 and
                sum(q.startswith("036-") for q, *_ in changes) == 30,
                "Expected exactly 35 L001 plus all 30 36 speaker corrections")
        require(changes[0][0] == "035-I-L-001", "35 source target changed")
        target_hash = expected_35_source_hash(conn, changes[0][2])
    else:
        require(len(records) == 2 and {r["key"] for r in records} == set(MARKERS),
                "Partial speaker correction markers; refuse to proceed")
        require(not changes, "Speaker markers exist but live dialogue is not normalized")
        marker = json.loads(records[0]["value"])
        target_hash = marker["updated_35_source_sha256"]
        require(all(json.loads(r["value"]) == marker for r in records),
                "Speaker marker provenance disagrees")
        require(marker["changed_transcripts"] == 31 and marker["rule_version"] == SPEAKER_TURN_RULE_VERSION and
                marker["old_35_source_sha256"] == ORIGINAL_SNAPSHOT_35 and
                marker["updated_35_source_sha256"] == FROZEN_35_SHA and
                marker["before_35_rows_sha256"] == EXPECTED_ROW_SHA["035-I-B"] and
                marker["before_36_rows_sha256"] == EXPECTED_ROW_SHA["036-I-B"],
                "Existing speaker marker does not match the live post-migration source")
        # Legitimate subsequent human review can change question/transcript
        # statuses, and the reviewer may edit already normalized dialogue.
        # Such edits must not invalidate the historical migration marker.
        guard = conn.execute(
            "SELECT t.tgenabled AS enabled,p.proname AS function_name "
            "FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid "
            "WHERE t.tgrelid='transcripts'::regclass "
            "AND t.tgname='topik_guard_speaker_colon_v1' AND NOT t.tgisinternal"
        ).fetchone()
        require(guard is not None and guard["enabled"] == "O" and
                guard["function_name"] == "topik_guard_speaker_colon_v1",
                "Speaker punctuation database guard missing or disabled")

    by_id = {r["question_id"]: r for r in before}
    for exam in EXAMS:
        for first in (25, 27, 29):
            pair = [by_id[f"{exam[:3]}-I-L-{n:03d}"] for n in (first, first + 1)]
            require(pair[0]["dialogue_text"] == pair[1]["dialogue_text"],
                    f"Shared transcript pair diverged: {exam} L{first}/{first+1}")

    history = invariants(conn)
    current35 = _snapshot_35(conn)
    require(current35 == (ORIGINAL_SNAPSHOT_35 if not records else target_hash),
            "35 source SHA is inconsistent with immutable source revision")
    updated = [{**r, "dialogue_text": normalize_speaker_turn_spacing(r["dialogue_text"])} for r in before]
    current_36_l010 = next(r["dialogue_text"] for r in before if r["question_id"] == "036-I-L-010")
    require("그래요? 그럼" in current_36_l010,
            "Existing human-approved 36 L010 punctuation correction is missing")
    report = {
        "status": "already_applied" if records else "pending",
        "speaker_rule": SPEAKER_TURN_RULE_VERSION,
        "affected_transcripts": len(changes),
        "by_exam": {exam: {"before_sha256": _row_hash(before, exam),
                          "after_sha256": _row_hash(updated, exam),
                          "changed_count": sum(q.startswith(exam[:3]) for q, *_ in changes)} for exam in EXAMS},
        "review_and_nontranscript_sha256": history,
        "historical_35_source_sha256": ORIGINAL_SNAPSHOT_35,
        "updated_35_source_sha256": target_hash,
        "reviewed_l010_preserved": True,
        "target_ids": [c[0] for c in changes],
    }
    report["preflight_sha256"] = checksum(report)
    return report, changes, before


def verified_backup(path: Path, proof_path: Path, expected_preflight: str) -> dict:
    require(path.is_absolute() and path.is_file() and path.stat().st_size > 0,
            "Backup must be a nonempty, absolute existing file")
    require(proof_path.is_file(), "Missing independent restore evidence")
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    with path.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    require(proof.get("backup_sha256") == sha and proof.get("preflight_sha256") == expected_preflight,
            "Backup SHA or restored source snapshot no longer matches live preflight")
    restored_database = proof.get("restored_database")
    require(proof.get("restore_verified") is True and proof.get("restore_details")
            and isinstance(restored_database, str)
            and restored_database.startswith("topik_speaker_restore_")
            and restored_database != "topik",
            "Independent disposable restore proof missing")
    try:
        backup_time = datetime.fromisoformat(proof["backed_up_at"])
        restored_at = datetime.fromisoformat(proof["restored_at"])
    except (KeyError, ValueError, TypeError) as error:
        raise MigrationBlocked("Backup/restore timestamps missing") from error
    now = datetime.now(timezone.utc)
    require(backup_time.tzinfo is not None and restored_at.tzinfo is not None and
            now - timedelta(hours=24) <= backup_time <= restored_at <= now + timedelta(minutes=2),
            "Backup or restore verification is older than 24 hours")
    return proof


# After correction, older unrefreshed reviewer processes must not reintroduce
# old speaker labels. This guard checks the current 35/36 transcript rows even
# if an old Python process ignores newer metadata marker versioning.
_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION topik_guard_speaker_colon_v1() RETURNS trigger
LANGUAGE plpgsql AS $func$
BEGIN
  IF NEW.question_id LIKE '035-I-L-%' OR NEW.question_id LIKE '036-I-L-%' THEN
    IF NEW.dialogue_text ~ E'(^|\\n)(남자|여자)[ \\t]+:'
       OR NEW.dialogue_text ~ E'(^|\\n)(남자|여자):[^ ]'
       OR NEW.dialogue_text ~ E'(^|\\n)(남자|여자):  +' THEN
      RAISE EXCEPTION 'Unnormalized transcript speaker punctuation: %', NEW.question_id
        USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END
$func$;
"""
_GUARD_TRIGGER = """CREATE TRIGGER topik_guard_speaker_colon_v1
BEFORE INSERT OR UPDATE ON transcripts FOR EACH ROW
EXECUTE FUNCTION topik_guard_speaker_colon_v1()"""


def run(*, apply=False, url=None, backup=None, restore_proof=None):
    conn = connect_postgres(url or get_database_url(required=True), readonly=not apply)
    try:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        if apply:
            conn.execute("SET LOCAL lock_timeout='8s'")
            conn.execute("SET LOCAL statement_timeout='60s'")
            conn.execute("LOCK TABLE exams,sections,source_files,questions,transcripts,"
                         "question_groups,choices,answers,images,question_images,"
                         "audio_assets,audio_segments,review_records,import_metadata "
                         "IN SHARE ROW EXCLUSIVE MODE")
        report, changes, before = inspect(conn)
        if not apply or report["status"] == "already_applied":
            conn.rollback()
            return report
        require(backup is not None and restore_proof is not None,
                "Live source update requires fresh backup and independently restored snapshot")
        verified_backup(Path(backup), Path(restore_proof), report["preflight_sha256"])
        previous_invariants = report["review_and_nontranscript_sha256"]
        for qid, older, newer in changes:
            result = conn.execute(
                "UPDATE transcripts SET dialogue_text=%s WHERE question_id=%s AND dialogue_text=%s",
                (newer, qid, older),
            )
            require(result.rowcount == 1, f"Transcript {qid} lost compare-and-swap race")
        conn.execute(_GUARD_FUNCTION)
        conn.execute(_GUARD_TRIGGER)
        marker = {"rule_version": SPEAKER_TURN_RULE_VERSION,
                  "old_35_source_sha256": ORIGINAL_SNAPSHOT_35,
                  "updated_35_source_sha256": report["updated_35_source_sha256"],
                  "preflight_sha256": report["preflight_sha256"],
                  "before_35_rows_sha256": report["by_exam"]["035-I-B"]["before_sha256"],
                  "before_36_rows_sha256": report["by_exam"]["036-I-B"]["before_sha256"],
                  "after_35_rows_sha256": report["by_exam"]["035-I-B"]["after_sha256"],
                  "after_36_rows_sha256": report["by_exam"]["036-I-B"]["after_sha256"],
                  "changed_transcripts": len(changes), "historical_audit_unchanged": True}
        marker_json = json.dumps(marker, ensure_ascii=False, sort_keys=True)
        for key in MARKERS:
            conn.execute("INSERT INTO import_metadata(key,value) VALUES(%s,%s)", (key, marker_json))
        now_rows = source_rows(conn)
        require(all(_row_hash(now_rows, exam) == report["by_exam"][exam]["after_sha256"] for exam in EXAMS),
                "Post-update transcripts differ from planned normalization")
        require(invariants(conn) == previous_invariants,
                "Nontranscript DB data or manual review history changed during correction")
        require(_snapshot_35(conn) == report["updated_35_source_sha256"],
                "35th updated source hash unexpected; rollback")
        conn.commit()
        return {**report, "status": "applied", "changed_transcripts": len(changes),
                "preserved_human_review_sha256": previous_invariants}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-file", type=Path)
    parser.add_argument("--restore-proof", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(run(apply=args.apply, backup=args.backup_file,
                             restore_proof=args.restore_proof), ensure_ascii=False, indent=2))
    except (MigrationBlocked, OSError, ValueError, KeyError) as exc:
        print(f"Speaker migration BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
