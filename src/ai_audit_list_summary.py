"""Bounded-query, read-only 35th AI audit summary for the question list.

``summarize_list_bulk(db_or_path)`` returns ``{question_id: ai_audit}`` where
each value is already in the compact ``ReviewStore.list_questions`` shape.
Only questions covered by at least one frozen pass are returned. It deliberately
does not load detail/history or call per-question audit helpers. For PostgreSQL,
pass a tuple-row PostgresAuditConnection (or URL); SQLite connections/paths are
supported too. All reads are pinned to one database snapshot.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
import json
from typing import Any, Iterator

from src import ai_audit_35 as audit


SEVERITY = {"low": 15, "medium": 35, "high": 65, "critical": 90}
ATTEMPT_STATUSES = ("failed", "invalid", "succeeded", "timed_out")


@contextmanager
def _snapshot(db: Any) -> Iterator[None]:
    if audit._is_postgres(db):
        # The tuple-row adapter uses a transactional connection. This must be
        # its first statement; the connection is not reused after the rollback.
        with audit._consistent_read(db):
            yield
    elif not db.in_transaction:
        db.execute("BEGIN")
        try:
            yield
        finally:
            db.rollback()
    else:
        # A caller-owned SQLite transaction already pins the read snapshot.
        yield


def _scope(raw: str) -> set[str]:
    return {item["id"] for item in json.loads(raw).get("subjects", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)}


def _level(score: int) -> str:
    return ("critical" if score >= 90 else "high" if score >= 70 else
            "medium" if score >= 40 else "low" if score > 0 else "none")


def _within_run(passes: list[dict], qid: str) -> dict:
    prior: set[str] = set()
    groups: list[set[str]] = []
    novelty: list[dict] = []
    for current_pass in passes:
        raw = current_pass["raw"]
        if raw is None or qid not in current_pass["scope"] or qid not in current_pass["verdicts"]:
            continue
        current = {item["fingerprint"] for item in raw.get("findings", []) if item.get("subject_id") == qid}
        new = current - prior
        novelty.append({"pass_id": current_pass["id"], "pass_number": current_pass["pass_number"],
                        "finding_count": len(current), "new_finding_count": len(new)})
        groups.append(current)
        prior.update(current)
    jaccard = None
    scope_match = None
    if len(groups) >= 2:
        union = groups[-1] | groups[-2]
        jaccard = round(len(groups[-1] & groups[-2]) / len(union), 4) if union else 1.0
        scope_match = True  # Both completed observations are projected onto this one subject.
    return {
        "completed_passes": len(groups),
        "new_findings_latest_pass": novelty[-1]["new_finding_count"] if novelty else 0,
        "converged": len(groups) >= 2 and scope_match is True and novelty[-1]["new_finding_count"] == 0,
        "latest_previous_jaccard": jaccard,
        "latest_previous_scope_match": scope_match,
        "novelty_by_pass": novelty,
    }


def _cross_run_convergence(runs: list[dict], by_run: dict[str, list[dict]],
                           run_fingerprints: dict[tuple[str, str], set[str]], qid: str) -> str:
    signatures = []
    for run in runs:
        current = [p for p in by_run[run["id"]] if qid in p["scope"]]
        if not current or any(qid not in p["verdicts"] for p in current):
            continue
        entries = [p["verdicts"][qid]["verdict"] for p in current if qid in p["verdicts"]]
        if not entries:
            continue
        signatures.append((tuple(sorted(set(entries))),
                           tuple(sorted(run_fingerprints.get((run["id"], qid), set())))))
        if len(signatures) == 2:
            break
    if len(signatures) < 2:
        return "insufficient_runs"
    return "stable" if signatures[0] == signatures[1] else "changed"


def _execution(qid: str, run: dict, passes: list[dict], attempts: dict[str, list[tuple]]) -> dict:
    relevant = [p for p in passes if qid in p["scope"]]
    counts = {status: 0 for status in ATTEMPT_STATUSES}
    retry = 0
    completed = 0
    for p in relevant:
        completed += p["raw"] is not None
        for number, status in attempts.get(p["id"], []):
            counts[status] += 1
            retry += number > 1
    latest_run = {
        "id": run["id"], "run_id": run["id"], "label": run["label"],
        "created_at": run["created_at"], "snapshot_sha256": run["snapshot_sha256"],
        "contract_version": run["contract_version"], "pass_total": len(relevant),
        "completed_passes": completed, "attempt_total": sum(counts.values()),
        "attempt_status_counts": counts, "subject_id": qid,
        "subject_ids": [qid], "subject_count": 1,
    }
    return {
        "latest_run": latest_run, "attempt_total": sum(counts.values()),
        "attempt_status_counts": counts, "retry_count": retry,
        "incomplete_passes": len(relevant) - completed,
        "has_partial_failures": any(counts[status] for status in ("failed", "timed_out", "invalid")),
    }


def summarize_list_bulk(db_or_path: Any) -> dict[str, dict]:
    """Get list-ready per-subject audit metadata in at most eight SELECTs.

    Mirrors ``summarize_question`` for verdicts, risk, within/cross-run
    convergence, and the F2 ``status_report`` subject-specific attempt rules.
    No tables are written and no per-question SQL is issued.
    """
    with audit._connection(db_or_path) as db:
        with _snapshot(db):
            if not audit.AI_AUDIT_TABLES.issubset(audit._table_names(db)):
                return {}
            qids = [row[0] for row in db.execute(
                "SELECT q.id FROM questions q JOIN sections s ON s.id=q.section_id "
                "WHERE s.exam_id=? ORDER BY q.exam_number", (audit.EXAM_ID,)).fetchall()]
            runs = [dict(zip(("id", "label", "created_at", "snapshot_sha256", "contract_version"), row))
                    for row in db.execute(
                        "SELECT r.id,r.label,r.created_at,r.snapshot_sha256,r.contract_version "
                        "FROM ai_audit_runs r WHERE r.exam_id=? "
                        f"ORDER BY {audit._run_order(db)}", (audit.EXAM_ID,)).fetchall()]
            if not runs:
                return {}
            # Fetch/parse large JSON once per pass, not once per question.
            raw_passes = db.execute(
                "SELECT p.id,p.run_id,p.pass_number,p.auditor_id,p.perspective,p.model_id,"
                "p.prompt_version,p.input_json,r.raw_json,r.created_at "
                "FROM ai_audit_passes p JOIN ai_audit_runs run ON run.id=p.run_id "
                "LEFT JOIN ai_audit_results r ON r.pass_id=p.id "
                "WHERE run.exam_id=? ORDER BY p.pass_number,p.id", (audit.EXAM_ID,)).fetchall()
            by_run: dict[str, list[dict]] = defaultdict(list)
            relevant_ids: set[str] = set()
            for row in raw_passes:
                scope = _scope(row[7])
                raw = json.loads(row[8]) if row[8] is not None else None
                verdicts = {item["subject_id"]: item for item in raw.get("verdicts", [])} if raw else {}
                p = {"id": row[0], "run_id": row[1], "pass_number": row[2],
                     "auditor_id": row[3], "perspective": row[4], "model_id": row[5],
                     "prompt_version": row[6], "scope": scope, "raw": raw,
                     "verdicts": verdicts, "result_created_at": row[9]}
                by_run[row[1]].append(p)
                relevant_ids.update(scope)

            occurrences: dict[tuple[str, str], dict[str, list[tuple]]] = defaultdict(lambda: defaultdict(list))
            run_fingerprints: dict[tuple[str, str], set[str]] = defaultdict(set)
            for row in db.execute(
                "SELECT p.run_id,f.subject_id,o.fingerprint,o.severity,o.summary,p.auditor_id "
                "FROM ai_audit_finding_occurrences o JOIN ai_audit_passes p ON p.id=o.pass_id "
                "JOIN ai_audit_findings f ON f.fingerprint=o.fingerprint "
                "JOIN ai_audit_runs run ON run.id=p.run_id WHERE run.exam_id=? ORDER BY o.id",
                (audit.EXAM_ID,)).fetchall():
                run_id, qid, fingerprint, severity, summary, auditor = row
                occurrences[(run_id, qid)][fingerprint].append((severity, summary, auditor))
                run_fingerprints[(run_id, qid)].add(fingerprint)

            attempts: dict[str, list[tuple]] = defaultdict(list)
            for pass_id, number, status in db.execute(
                "SELECT a.pass_id,a.attempt_number,a.status FROM ai_audit_attempts a "
                "JOIN ai_audit_passes p ON p.id=a.pass_id "
                "JOIN ai_audit_runs run ON run.id=p.run_id WHERE run.exam_id=? "
                "ORDER BY p.pass_number,a.attempt_number,a.id", (audit.EXAM_ID,)).fetchall():
                attempts[pass_id].append((number, status))

            # No checkpoint query is required: list execution uses completed
            # results and attempts; only detail exposes checkpoint progress.
            summaries = {}
            for qid in qids:
                if qid not in relevant_ids:
                    continue
                result_run = next((r for r in runs if any(qid in p["verdicts"] for p in by_run[r["id"]])), None)
                summary: dict[str, Any] = {}
                if result_run is not None:
                    run_id = result_run["id"]
                    passes = by_run[run_id]
                    entries = [p for p in passes if qid in p["verdicts"]]
                    counts = {verdict: sum(p["verdicts"][qid]["verdict"] == verdict for p in entries)
                              for verdict in audit.VERDICTS}
                    findings = occurrences.get((run_id, qid), {})
                    disagreement = len({p["verdicts"][qid]["verdict"] for p in entries}) > 1
                    risk = max((SEVERITY[item[0]] for seen in findings.values() for item in seen), default=0)
                    recurrence = max((len(seen) for seen in findings.values()), default=0)
                    if recurrence > 1:
                        risk += min(20, (recurrence - 1) * 10)
                    if disagreement:
                        risk += 15
                    if counts["uncertain"]:
                        risk += min(10, counts["uncertain"] * 5)
                    risk = min(100, risk)
                    eligible = [p for p in passes if qid in p["scope"]]
                    completed = sum(qid in p["verdicts"] for p in eligible)
                    latest_run = {
                        "id": run_id, "run_id": run_id, "label": result_run["label"],
                        "created_at": result_run["created_at"],
                        "snapshot_sha256": result_run["snapshot_sha256"],
                        "pass_total": len(eligible), "completed_passes": completed,
                        "summary": f"{completed}/{len(eligible)} passes completed",
                    }
                    summary = {
                        "run_id": run_id, "total": len(entries), **counts,
                        "unresolved_findings": len(findings), "disagreement": disagreement,
                        "risk_score": risk, "risk_level": _level(risk),
                        "convergence": _within_run(passes, qid),
                        "cross_run_convergence": _cross_run_convergence(runs, by_run, run_fingerprints, qid),
                        "latest_run": latest_run,
                        "latest_at": entries[-1]["result_created_at"] or result_run["created_at"],
                    }
                # Preserve the existing list selection: prefer the newest run
                # containing verdicts, otherwise newest pending scoped run.
                execution_run = result_run or next(
                    (r for r in runs if any(qid in p["scope"] for p in by_run[r["id"]])), None)
                if execution_run is not None:
                    summary.update(_execution(qid, execution_run, by_run[execution_run["id"]], attempts))
                if summary:
                    summaries[qid] = summary
            return summaries
