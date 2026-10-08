"""Finalize the human-confirmed 35-I text extraction review after full blind audit.

The user already reviewed the 70 questions and confirmed source-backed fixes.
This helper never approves from a partial or stale AI result. The only
operational write path is the existing versioned ReviewStore.save_review API.

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
from src.database import connect_postgres, get_database_url
from src.review_ui import ReviewStore


RUN_ID = "audit35-f05e8a9febf348b2"
PASS_ID = "audit35-f05e8a9febf348b2-p1-ee94537a"
SOURCE_SNAPSHOT_SHA256 = "631c4fb71784439961956b582d0e66847eb862e2c2370f49c89d5ca520057c35"
INPUT_SHA256 = "d509e54faea7982fd01cabb3b5dbd1cc4e8c7b5b9ada39841a2f562b48767249"
REVIEW_NOTE = (
    "35회차 문제/텍스트 추출 최종 검수: 사용자가 전체 문항을 검토하고 "
    "원본 기반 오류 보정을 확인함. source-backed correction 이후 70문항 "
    f"독립 blind audit {RUN_ID} 전부 clear. 음원 candidate 구간 승인은 별도 작업."
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
    baseline = check()
    store = ReviewStore()
    completed = []
    for question_id in baseline["pending"]:
        question = store.get_question(question_id, fast=True)
        if question["review_status"] != "needs_manual_review":
            raise RuntimeError(f"question state changed during finalization: {question_id}")
        payload = {
            "version": question["version"],
            "status": "verified",
            "stem": question["stem"],
            "choices": [item["text"] for item in question["choices"]],
            "transcript_text": (
                question["transcript"]["text"] if question["transcript"] else None
            ),
            "note": REVIEW_NOTE,
        }
        saved = store.save_review(question_id, payload, fast_response=True)
        if not saved["saved"] or saved["review_status"] != "verified":
            raise RuntimeError(f"review approval not confirmed: {question_id}")
        completed.append(question_id)
    verified = check()
    if not verified["approved"]:
        raise RuntimeError("final human review status not fully verified")
    return {"approved_now": completed, "approved_now_count": len(completed), **verified}


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
