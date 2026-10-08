"""Apply the source-backed 35th TOPIK I corrections found in the final text audit.

The Stage-10 SQLite database is an immutable archive.  This helper only writes
the operational PostgreSQL database configured by ``TOPIK_DATABASE_URL``.

Run ``check`` first.  ``apply`` is fail-closed: every target value must still
match the audited pre-correction value, source PDFs must still match the hashes
recorded in PostgreSQL, and unrelated scored/media data must remain unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.database import connect_postgres, get_database_url, get_media_root


CORRECTION_VERSION = "audit35-gpt6-third-pass-source-correction-v1"
METADATA_KEY = "audit35_source_correction_version"
REVIEW_SCOPE = "source_backed_correction_35"
REVIEWER = "source_correction"

EXPECTED_GROUPS = {
    "035-I-L-025": "035-I-L-25-26",
    "035-I-L-026": "035-I-L-25-26",
    "035-I-L-027": "035-I-L-27-28",
    "035-I-L-028": "035-I-L-27-28",
    "035-I-L-029": "035-I-L-29-30",
    "035-I-L-030": "035-I-L-29-30",
    **{f"035-I-R-{number:03d}": "035-I-R-31-33" for number in range(31, 34)},
    **{f"035-I-R-{number:03d}": "035-I-R-34-39" for number in range(34, 40)},
    "035-I-R-045": "035-I-R-43-45",
    "035-I-R-057": "035-I-R-57-58",
    "035-I-R-058": "035-I-R-57-58",
    "035-I-R-067": "035-I-R-67-68",
    "035-I-R-068": "035-I-R-67-68",
}


QUESTION_CORRECTIONS: dict[str, dict[str, Any]] = {
    "035-I-L-025": {
        "before": "",
        "after": "어떤 이야기를 하고 있는지 고르십시오.",
        "source": "35th-TOPIK-I-Listening-Transcript.pdf",
        "page": 10,
    },
    "035-I-L-026": {
        "before": "",
        "after": "들은 내용과 같은 것을 고르십시오.",
        "source": "35th-TOPIK-I-Listening-Transcript.pdf",
        "page": 10,
    },
    "035-I-L-027": {
        "before": "",
        "after": "두 사람이 무엇에 대해 이야기를 하고 있는지 고르십시오.",
        "source": "35th-TOPIK-I-Listening-Transcript.pdf",
        "page": 11,
    },
    "035-I-L-028": {
        "before": "",
        "after": "들은 내용과 같은 것을 고르십시오.",
        "source": "35th-TOPIK-I-Listening-Transcript.pdf",
        "page": 11,
    },
    "035-I-L-029": {
        "before": "",
        "after": "두 사람은 왜 김치를 만듭니까?",
        "source": "35th-TOPIK-I-Listening-Transcript.pdf",
        "page": 12,
    },
    "035-I-L-030": {
        "before": "",
        "after": "들은 내용과 같은 것을 고르십시오.",
        "source": "35th-TOPIK-I-Listening-Transcript.pdf",
        "page": 12,
    },
    "035-I-R-034": {
        "before": "몇 시( )옵니까?",
        "after": "몇 시( ) 옵니까?",
        "source": "35th-TOPIK-I-Papers.pdf",
        "page": 12,
    },
    "035-I-R-035": {
        "before": "( ) 에 갑니다. 우유를 삽니다.",
        "after": "( )에 갑니다. 우유를 삽니다.",
        "source": "35th-TOPIK-I-Papers.pdf",
        "page": 12,
    },
    "035-I-R-038": {
        "before": "산을 좋아합니다. 그래서 등산을 ( )합니다.",
        "after": "산을 좋아합니다. 그래서 등산을 ( ) 합니다.",
        "source": "35th-TOPIK-I-Papers.pdf",
        "page": 13,
    },
    "035-I-R-039": {
        "before": "머리가 깁니다. 그래서 ( )싶습니다.",
        "after": "머리가 깁니다. 그래서 ( ) 싶습니다.",
        "source": "35th-TOPIK-I-Papers.pdf",
        "page": 13,
    },
    "035-I-R-045": {
        "before": "친구가 지난달에 고향으로 돌아갔습니다. 친구는 저에게 냉장고를 주었 습니다. 그 냉장고는 커서 좋습니다.",
        "after": "친구가 지난달에 고향으로 돌아갔습니다. 친구는 저에게 냉장고를 주었습니다. 그 냉장고는 커서 좋습니다.",
        "source": "35th-TOPIK-I-Papers.pdf",
        "page": 15,
    },
    "035-I-R-057": {
        "before": "(가)모든 동물은 잠을 잡니다. (나)하지만 개나 고양이는 열 시간쯤 잡니다. (다)말은 하루에 세 시간만 자도 괜찮습니다. (라)그런데 잠을 자는 시간은 동물마다 다릅니다.",
        "after": "(가) 모든 동물은 잠을 잡니다. (나) 하지만 개나 고양이는 열 시간쯤 잡니다. (다) 말은 하루에 세 시간만 자도 괜찮습니다. (라) 그런데 잠을 자는 시간은 동물마다 다릅니다.",
        "source": "35th-TOPIK-I-Papers.pdf",
        "page": 21,
    },
    "035-I-R-058": {
        "before": "(가)우리 고향에는 딸기가 많이 납니다. (나)그래서 딸기가 많은 4월에 축제를 합니다. (다)그리고 맛있는 딸기를 시장보다 싸게 살 수 있습니다. (라)이 축제에서는 딸기로 여러 가지 음식을 만들어 볼 수 있습니다.",
        "after": "(가) 우리 고향에는 딸기가 많이 납니다. (나) 그래서 딸기가 많은 4월에 축제를 합니다. (다) 그리고 맛있는 딸기를 시장보다 싸게 살 수 있습니다. (라) 이 축제에서는 딸기로 여러 가지 음식을 만들어 볼 수 있습니다.",
        "source": "35th-TOPIK-I-Papers.pdf",
        "page": 21,
    },
}


GROUP_CORRECTIONS: dict[str, dict[str, Any]] = {
    "035-I-R-31-33": {
        "field": "instruction",
        "before": "무엇에 대한 이야기입니까?<보기>와 같이 알맞은 것을 고르 십시오.",
        "after": "무엇에 대한 이야기입니까? <보기>와 같이 알맞은 것을 고르십시오.",
        "questions": ("035-I-R-031", "035-I-R-032", "035-I-R-033"),
        "source": "35th-TOPIK-I-Papers.pdf",
        "page": 11,
    },
    "035-I-R-34-39": {
        "field": "instruction",
        "before": "<보기>와 같이 ( ) 에 들어갈 가장 알맞은 것을 고르 십시오.",
        "after": "<보기>와 같이 ( )에 들어갈 가장 알맞은 것을 고르십시오.",
        "questions": tuple(f"035-I-R-{number:03d}" for number in range(34, 40)),
        "source": "35th-TOPIK-I-Papers.pdf",
        "page": 12,
    },
    "035-I-R-67-68": {
        "field": "passage_text",
        "before": "문제를 풀기 어려울 때는 책상 앞에만 앉아 있지 마십시오. 계속 앉아 있으면 좋은 생각이 ( ㉠ )않습니다. 그럴 때는 일어나서 걷는 것이 좋습니다. 걸으려고 꼭 밖으로 ( ㉡ ). 집 안도 좋고 사무실 안도 괜찮 습니다.",
        "after": "문제를 풀기 어려울 때는 책상 앞에만 앉아 있지 마십시오. 계속 앉아 있으면 좋은 생각이 ( ㉠ )않습니다. 그럴 때는 일어나서 걷는 것이 좋습니다. 걸으려고 꼭 밖으로 ( ㉡ ). 집 안도 좋고 사무실 안도 괜찮습니다.",
        "questions": ("035-I-R-067", "035-I-R-068"),
        "source": "35th-TOPIK-I-Papers.pdf",
        "page": 26,
    },
}


def affected_question_ids() -> tuple[str, ...]:
    ids = set(QUESTION_CORRECTIONS)
    for correction in GROUP_CORRECTIONS.values():
        ids.update(correction["questions"])
    return tuple(sorted(ids))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_path(filename: str) -> Path:
    return get_media_root() / "35th" / filename


def _verify_source(conn, filename: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT relative_path,sha256,byte_size FROM source_files WHERE relative_path LIKE %s",
        (f"%/35th/{filename}",),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"source file is not registered in PostgreSQL: {filename}")
    path = _source_path(filename)
    if not path.is_file():
        raise RuntimeError(f"source file is missing locally: {path}")
    actual_sha = _sha256(path)
    if path.stat().st_size != row["byte_size"] or actual_sha != row["sha256"]:
        raise RuntimeError(f"source file changed since import: {filename}")
    return {"file": filename, "sha256": actual_sha, "byte_size": path.stat().st_size}


def _canonical_rows(rows) -> str:
    values = []
    for row in rows:
        if hasattr(row, "keys"):
            values.append({key: row[key] for key in row.keys()})
        else:
            values.append(list(row))
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _protected_state(conn) -> dict[str, str]:
    queries = {
        "choices": "SELECT question_id,number,text FROM choices ORDER BY question_id,number",
        "answers": "SELECT question_id,choice_number,source_file_id,source_pdf_page,preview_and_pdf_agree FROM answers ORDER BY question_id",
        "question_images": "SELECT question_id,image_key FROM question_images ORDER BY question_id,image_key",
        "transcripts": "SELECT question_id,source_file_id,source_pdf_page,dialogue_text,review_status,warnings_json FROM transcripts ORDER BY question_id",
        "audio_segments": "SELECT question_id,audio_asset_id,start_ms,end_ms,status,version,source_sha256,updated_at,clip_relative_path,clip_sha256 FROM audio_segments ORDER BY question_id",
    }
    state = {}
    for name, sql in queries.items():
        encoded = _canonical_rows(conn.execute(sql).fetchall()).encode("utf-8")
        state[name] = hashlib.sha256(encoded).hexdigest()
    return state


def _read_targets(conn, *, lock: bool) -> tuple[dict[str, dict], dict[str, dict]]:
    qids = affected_question_ids()
    placeholders = ",".join(["%s"] * len(qids))
    suffix = " FOR UPDATE" if lock else ""
    question_rows = conn.execute(
        f"SELECT id,group_id,stem,review_status FROM questions WHERE id IN ({placeholders}) ORDER BY id{suffix}",
        qids,
    ).fetchall()
    if len(question_rows) != len(qids):
        raise RuntimeError("one or more correction questions are missing")
    questions = {row["id"]: dict(row) for row in question_rows}

    group_ids = tuple(sorted(GROUP_CORRECTIONS))
    placeholders = ",".join(["%s"] * len(group_ids))
    group_rows = conn.execute(
        f"SELECT id,instruction,passage_text FROM question_groups WHERE id IN ({placeholders}) ORDER BY id{suffix}",
        group_ids,
    ).fetchall()
    if len(group_rows) != len(group_ids):
        raise RuntimeError("one or more correction groups are missing")
    groups = {row["id"]: dict(row) for row in group_rows}
    return questions, groups


def _validate_group_membership(questions: dict[str, dict]) -> None:
    for qid, expected_group in EXPECTED_GROUPS.items():
        if questions[qid]["group_id"] != expected_group:
            raise RuntimeError(
                f"{qid} group changed: expected {expected_group}, got {questions[qid]['group_id']}"
            )


def _target_state(questions: dict[str, dict], groups: dict[str, dict]) -> str:
    states: set[str] = set()
    for qid, correction in QUESTION_CORRECTIONS.items():
        value = questions[qid]["stem"]
        if value == correction["before"]:
            states.add("before")
        elif value == correction["after"]:
            states.add("after")
        else:
            raise RuntimeError(f"{qid} stem no longer matches audited before/after values")
    for gid, correction in GROUP_CORRECTIONS.items():
        value = groups[gid][correction["field"]]
        if value == correction["before"]:
            states.add("before")
        elif value == correction["after"]:
            states.add("after")
        else:
            raise RuntimeError(f"{gid}.{correction['field']} no longer matches audited before/after values")
    if len(states) != 1:
        raise RuntimeError("correction targets are partially applied; manual reconciliation required")
    return next(iter(states))


def check(url: str | None = None) -> dict[str, Any]:
    resolved = url or get_database_url(required=True)
    conn = connect_postgres(resolved, readonly=True)
    try:
        questions, groups = _read_targets(conn, lock=False)
        _validate_group_membership(questions)
        target_state = _target_state(questions, groups)
        source_files = [
            _verify_source(conn, "35th-TOPIK-I-Papers.pdf"),
            _verify_source(conn, "35th-TOPIK-I-Listening-Transcript.pdf"),
        ]
        metadata = conn.execute(
            "SELECT value FROM import_metadata WHERE key=%s", (METADATA_KEY,)
        ).fetchone()
        question_status = {
            row["review_status"]: row["count"]
            for row in conn.execute(
                "SELECT review_status,COUNT(*) AS count FROM questions GROUP BY review_status ORDER BY review_status"
            ).fetchall()
        }
        transcript_status = {
            row["review_status"]: row["count"]
            for row in conn.execute(
                "SELECT review_status,COUNT(*) AS count FROM transcripts GROUP BY review_status ORDER BY review_status"
            ).fetchall()
        }
        audio_status = {
            row["status"]: row["count"]
            for row in conn.execute(
                "SELECT status,COUNT(*) AS count FROM audio_segments GROUP BY status ORDER BY status"
            ).fetchall()
        }
        correction_records = conn.execute(
            "SELECT COUNT(*) AS count FROM review_records WHERE scope=%s", (REVIEW_SCOPE,)
        ).fetchone()["count"]
        listening_rows = conn.execute(
            "SELECT q.exam_number,q.stem,q.review_status AS question_status,t.review_status AS transcript_status "
            "FROM questions q JOIN transcripts t ON t.question_id=q.id "
            "WHERE q.exam_number BETWEEN 25 AND 30 ORDER BY q.exam_number"
        ).fetchall()
        return {
            "correction_version": CORRECTION_VERSION,
            "target_state": target_state,
            "metadata": metadata["value"] if metadata else None,
            "affected_questions": list(affected_question_ids()),
            "affected_question_count": len(affected_question_ids()),
            "direct_stem_corrections": len(QUESTION_CORRECTIONS),
            "group_corrections": len(GROUP_CORRECTIONS),
            "source_files": source_files,
            "question_status": question_status,
            "transcript_status": transcript_status,
            "audio_status": audio_status,
            "correction_review_records": correction_records,
            "listening_25_30": [dict(row) for row in listening_rows],
        }
    finally:
        conn.close()


def apply(url: str | None = None) -> dict[str, Any]:
    resolved = url or get_database_url(required=True)
    conn = connect_postgres(resolved, readonly=False)
    try:
        before_protected = _protected_state(conn)
        _verify_source(conn, "35th-TOPIK-I-Papers.pdf")
        _verify_source(conn, "35th-TOPIK-I-Listening-Transcript.pdf")
        questions, groups = _read_targets(conn, lock=True)
        _validate_group_membership(questions)
        state = _target_state(questions, groups)
        metadata = conn.execute(
            "SELECT value FROM import_metadata WHERE key=%s FOR UPDATE", (METADATA_KEY,)
        ).fetchone()
        if metadata is not None:
            if metadata["value"] != CORRECTION_VERSION:
                raise RuntimeError("a different source-correction version is already recorded")
            if state != "after":
                raise RuntimeError("correction metadata exists but target values do not match it")
            conn.rollback()
            return {
                "correction_version": CORRECTION_VERSION,
                "already_applied": True,
                "affected_question_count": len(affected_question_ids()),
            }
        if state != "before":
            raise RuntimeError("target values are already corrected without matching provenance metadata")

        original_status = {qid: questions[qid]["review_status"] for qid in affected_question_ids()}
        evidence_changes: dict[str, list[dict[str, Any]]] = {qid: [] for qid in affected_question_ids()}

        for qid, correction in QUESTION_CORRECTIONS.items():
            cursor = conn.execute(
                "UPDATE questions SET stem=%s,review_status='needs_manual_review' WHERE id=%s AND stem=%s",
                (correction["after"], qid, correction["before"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent question change blocked correction: {qid}")
            evidence_changes[qid].append({
                "field": "questions.stem",
                "before": correction["before"],
                "after": correction["after"],
                "source": correction["source"],
                "source_pdf_page": correction["page"],
            })

        for gid, correction in GROUP_CORRECTIONS.items():
            field = correction["field"]
            cursor = conn.execute(
                f"UPDATE question_groups SET {field}=%s WHERE id=%s AND {field}=%s",
                (correction["after"], gid, correction["before"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent group change blocked correction: {gid}")
            for qid in correction["questions"]:
                conn.execute("UPDATE questions SET review_status='needs_manual_review' WHERE id=%s", (qid,))
                evidence_changes[qid].append({
                    "field": f"question_groups.{field}",
                    "group_id": gid,
                    "before": correction["before"],
                    "after": correction["after"],
                    "source": correction["source"],
                    "source_pdf_page": correction["page"],
                })

        reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for qid in affected_question_ids():
            evidence = {
                "correction_version": CORRECTION_VERSION,
                "originating_audit": "chatgpt-gpt-6-third-independent-pass",
                "reason": "Human-confirmed source fidelity corrections from the 2026-10-08 blind audit",
                "prior_review_status": original_status[qid],
                "changes": evidence_changes[qid],
            }
            conn.execute(
                "INSERT INTO review_records(subject_type,subject_id,status,reviewer,scope,evidence,reviewed_at) "
                "VALUES('question',%s,'needs_manual_review',%s,%s,%s,%s)",
                (qid, REVIEWER, REVIEW_SCOPE, json.dumps(evidence, ensure_ascii=False), reviewed_at),
            )

        conn.execute(
            "INSERT INTO import_metadata(key,value) VALUES(%s,%s)",
            (METADATA_KEY, CORRECTION_VERSION),
        )

        after_questions, after_groups = _read_targets(conn, lock=False)
        _validate_group_membership(after_questions)
        if _target_state(after_questions, after_groups) != "after":
            raise RuntimeError("post-correction target verification failed")
        if any(after_questions[qid]["review_status"] != "needs_manual_review" for qid in affected_question_ids()):
            raise RuntimeError("one or more changed questions were not returned to manual review")

        after_protected = _protected_state(conn)
        if before_protected != after_protected:
            raise RuntimeError("protected choices/answers/images/transcripts/audio state changed")

        # Local source files are outside the PostgreSQL transaction, so verify
        # them again immediately before commit to close the check/use window.
        _verify_source(conn, "35th-TOPIK-I-Papers.pdf")
        _verify_source(conn, "35th-TOPIK-I-Listening-Transcript.pdf")

        conn.commit()
        return {
            "correction_version": CORRECTION_VERSION,
            "already_applied": False,
            "affected_questions": list(affected_question_ids()),
            "affected_question_count": len(affected_question_ids()),
            "direct_stem_corrections": len(QUESTION_CORRECTIONS),
            "group_corrections": len(GROUP_CORRECTIONS),
            "protected_state_sha256": after_protected,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "apply"))
    args = parser.parse_args(argv)
    try:
        result = check() if args.command == "check" else apply()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"35th TOPIK source correction blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
