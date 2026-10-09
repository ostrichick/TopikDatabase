"""Finalize the human-confirmed 35-I text extraction review after full blind audit.

The user already reviewed the 70 questions and confirmed source-backed fixes.
This helper never approves from a partial or stale AI result. Finalization is
one PostgreSQL transaction, preserving existing transcript/audio status and
recording explicitly user-authorized automated approvals.

Usage:
    py -3 scripts/finalize_blind_audit_35.py check
    py -3 scripts/finalize_blind_audit_35.py apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import ai_audit_35
from src.database import PostgresAuditConnection, connect_postgres, get_database_url


RUN_ID = "audit35-f05e8a9febf348b2"
PASS_ID = "audit35-f05e8a9febf348b2-p1-ee94537a"
SOURCE_SNAPSHOT_SHA256 = "631c4fb71784439961956b582d0e66847eb862e2c2370f49c89d5ca520057c35"
INPUT_SHA256 = "d509e54faea7982fd01cabb3b5dbd1cc4e8c7b5b9ada39841a2f562b48767249"
REVIEW_NOTE = (
    "User-authorized completion of 35th TOPIK I question/text extraction: "
    "the user examined the extracted exam and confirmed the source-backed "
    f"correction findings; independent blind audit {RUN_ID} returned 70/70 clear. "
    "Applied by automation, not a new per-question human inspection. "
    "MP3 segment timing/audio playback verification is a separate task."
)


def _validate_result(response: dict, subject_ids: set[str]) -> None:
    if response.get("contract_version") != ai_audit_35.CONTRACT_VERSION:
        raise RuntimeError("final audit contract version mismatch")
    if response.get("pass_id") != PASS_ID or response.get("input_sha256") != INPUT_SHA256:
        raise RuntimeError("final audit input identity mismatch")
    completed = response.get("completed_subject_ids")
    verdicts = response.get("verdicts")
    if not isinstance(completed, list) or len(completed) != 70 or set(completed) != subject_ids:
        raise RuntimeError("final audit did not complete exactly the 70 expected questions")
    if len(set(completed)) != 70 or not isinstance(verdicts, list) or len(verdicts) != 70:
        raise RuntimeError("final audit contains missing/duplicate judgments")
    verdict_ids = [item.get("subject_id") for item in verdicts if isinstance(item, dict)]
    if len(verdict_ids) != 70 or len(set(verdict_ids)) != 70 or set(verdict_ids) != subject_ids:
        raise RuntimeError("final audit verdict coverage mismatch")
    if any(item.get("verdict") != "clear" for item in verdicts):
        raise RuntimeError("final audit has finding or uncertain judgments; approval blocked")
    if response.get("findings") != []:
        raise RuntimeError("final audit contains unresolved findings; approval blocked")


def check() -> dict:
    url = get_database_url(required=True)
    db = connect_postgres(url, readonly=True)
    try:
        row = db.execute(
            "SELECT r.snapshot_sha256, p.input_sha256, p.perspective, res.raw_json "
            "FROM ai_audit_runs r JOIN ai_audit_passes p ON p.run_id=r.id "
            "LEFT JOIN ai_audit_results res ON res.pass_id=p.id "
            "WHERE r.id=%s AND p.id=%s",
            (RUN_ID, PASS_ID),
        ).fetchone()
        if row is None or row["raw_json"] is None:
            raise RuntimeError("final v4 independent blind audit result not imported")
        if row["snapshot_sha256"] != SOURCE_SNAPSHOT_SHA256:
            raise RuntimeError("audit run snapshot identity mismatch")
        if row["input_sha256"] != INPUT_SHA256 or row["perspective"] != "independent":
            raise RuntimeError("expected independent pass identity mismatch")
        response = json.loads(row["raw_json"])
        rows = db.execute(
            "SELECT id,review_status FROM questions ORDER BY exam_number"
        ).fetchall()
        question_ids = {item["id"] for item in rows}
        if len(rows) != 70 or len(question_ids) != 70:
            raise RuntimeError("operational question corpus is not the expected 70 items")
        if any(item["review_status"] not in ("verified", "needs_manual_review") for item in rows):
            raise RuntimeError("rejected or unexpected human review status blocks completion")
        _validate_result(response, question_ids)
        pending = [item["id"] for item in rows if item["review_status"] == "needs_manual_review"]
    finally:
        db.close()

    # Source snapshots intentionally exclude human review state, allowing
    # verified statuses to change without invalidating the content digest.
    live_snapshot = ai_audit_35.create_source_snapshot(url)["snapshot_sha256"]
    if live_snapshot != SOURCE_SNAPSHOT_SHA256:
        raise RuntimeError("operational content differs from the final blind audited snapshot")
    return {
        "run_id": RUN_ID,
        "snapshot_sha256": SOURCE_SNAPSHOT_SHA256,
        "clear": 70,
        "finding": 0,
        "uncertain": 0,
        "verified": 70 - len(pending),
        "pending": pending,
        "approved": len(pending) == 0,
    }


def apply() -> dict:
    url = get_database_url(required=True)
    db = PostgresAuditConnection(url, readonly=False)
    try:
        # Prevent any changes to the frozen audit source while comparing the
        # full content snapshot and committing all approval/history rows.
        # SELECTs by reviewers remain available; competing writes wait.
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        db.execute(
            "LOCK TABLE exams,sections,source_files,question_groups,questions,"
            "choices,answers,images,question_images,transcripts,audio_assets,"
            "audio_segments IN SHARE ROW EXCLUSIVE MODE"
        )

        audit_row = db.execute(
            "SELECT r.snapshot_sha256,p.input_sha256,p.perspective,res.raw_json "
            "FROM ai_audit_runs r JOIN ai_audit_passes p ON p.run_id=r.id "
            "JOIN ai_audit_results res ON res.pass_id=p.id "
            "WHERE r.id=? AND p.id=?",
            (RUN_ID, PASS_ID),
        ).fetchone()
        if audit_row is None:
            raise RuntimeError("final v4 independent blind audit result not imported")
        snapshot_sha, input_sha, perspective, raw_json = audit_row
        if snapshot_sha != SOURCE_SNAPSHOT_SHA256 or input_sha != INPUT_SHA256:
            raise RuntimeError("frozen audit run/pass identity mismatch")
        if perspective != "independent":
            raise RuntimeError("final pass must be independently audited")

        questions = db.execute(
            "SELECT id,review_status FROM questions ORDER BY id"
        ).fetchall()
        subject_ids = {item[0] for item in questions}
        if len(questions) != 70 or len(subject_ids) != 70:
            raise RuntimeError("operational corpus is not exactly 70 unique questions")
        if any(status not in ("verified", "needs_manual_review") for _, status in questions):
            raise RuntimeError("rejected or unexpected review status blocks finalization")
        _validate_result(json.loads(raw_json), subject_ids)

        transcript_statuses = db.execute(
            "SELECT question_id,review_status FROM transcripts ORDER BY question_id"
        ).fetchall()
        if len(transcript_statuses) != 30 or any(
            status != "verified" for _, status in transcript_statuses
        ):
            raise RuntimeError("all 30 human-verified listening transcripts must remain verified")

        # This is the actual source snapshot check inside the SAME database
        # transaction that applies the approvals; there is no TOCTOU window.
        current_snapshot = ai_audit_35._source_snapshot_payload(db)
        if ai_audit_35._sha(current_snapshot) != SOURCE_SNAPSHOT_SHA256:
            raise RuntimeError("frozen source differs from operational content; nothing approved")

        pending = [qid for qid, status in questions if status == "needs_manual_review"]
        if pending:
            changed = db.execute(
                "UPDATE questions SET review_status='verified' "
                "WHERE id=ANY(?) AND review_status='needs_manual_review' RETURNING id",
                (pending,),
            ).fetchall()
            if {row[0] for row in changed} != set(pending):
                raise RuntimeError("stale question status prevented atomic finalization")
            cursor = db.execute(
                "INSERT INTO review_records "
                "(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                "SELECT 'question',q.id,'verified','user_authorized_bulk_finalizer',"
                "'manual_question_review',"
                "jsonb_build_object("
                "'note',?::text,"
                "'automation',true,"
                "'audit_run_id',?::text,"
                "'before',jsonb_build_object('status','needs_manual_review'),"
                "'after',jsonb_build_object('status','verified')"
                ")::text,"
                "to_char(clock_timestamp() AT TIME ZONE 'UTC', "
                "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"') "
                "FROM questions q WHERE q.id=ANY(?) ORDER BY q.id",
                (REVIEW_NOTE, RUN_ID, pending),
            )
            if cursor.rowcount != len(pending):
                raise RuntimeError("missing approval history rows; rolling back all changes")
        status_rows = db.execute(
            "SELECT review_status,COUNT(*) FROM questions GROUP BY review_status"
        ).fetchall()
        if dict(status_rows) != {"verified": 70}:
            raise RuntimeError("not all 70 questions verified; rolling back all changes")

        # Do not change transcript, answer, choice, image or audio rows.
        db.commit()
        return {
            "run_id": RUN_ID,
            "snapshot_sha256": SOURCE_SNAPSHOT_SHA256,
            "clear": 70, "finding": 0, "uncertain": 0,
            "verified": 70, "pending": [], "approved": True,
            "approved_now": pending, "approved_now_count": len(pending),
            "atomic": True,
        }
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "apply"))
    args = parser.parse_args(argv)
    try:
        result = check() if args.command == "check" else apply()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"35-I final audit approval blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
