"""Mutating Stage 9 PostgreSQL smoke for disposable candidate/restore databases.

The database target is intentionally mutated. Local PDF/MP3 evidence is copied
into temporary hard-link media roots, so originals and the canonical SQLite
source remain untouched. Do not point this helper at the post-cutover clean DB.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import ai_audit_35
from src.database import connect_postgres
from src.review_ui import Conflict, ReviewStore


SOURCE_DB = ROOT / "topik-past-papers" / "derived" / "035-I-B.sqlite"
SOURCE_MEDIA_ROOT = ROOT / "topik-past-papers"


def _materialize_media_root(destination: Path) -> None:
    db = sqlite3.connect(SOURCE_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("SELECT relative_path FROM source_files ORDER BY id").fetchall()
    finally:
        db.close()
    for (relative,) in rows:
        logical = Path(relative)
        if logical.parts and logical.parts[0] == "topik-past-papers":
            logical = Path(*logical.parts[1:])
        source = (SOURCE_MEDIA_ROOT / logical).resolve()
        target = (destination / logical).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)


def _review_payload(detail: dict, note: str) -> dict:
    return {
        "version": detail["version"],
        "status": detail["review_status"],
        "stem": detail["stem"],
        "choices": [item["text"] for item in detail["choices"]],
        "transcript_text": detail["transcript"]["text"] if detail["transcript"] else None,
        "note": note,
    }


def run_smoke(url: str) -> dict:
    if not url.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("Stage 9 smoke requires an explicit PostgreSQL URL")

    with tempfile.TemporaryDirectory(prefix="topik-stage9-media-a-") as first_dir, \
            tempfile.TemporaryDirectory(prefix="topik-stage9-media-b-") as second_dir:
        first_root = Path(first_dir)
        second_root = Path(second_dir)
        _materialize_media_root(first_root)
        _materialize_media_root(second_root)
        first = ReviewStore(root=ROOT, database_url=url, media_root=first_root)
        second = ReviewStore(root=ROOT, database_url=url, media_root=second_root)

        # Human review: first write succeeds, a second client using the stale
        # version fails with the existing Stage-6 optimistic conflict contract.
        qid = "035-I-R-031"
        initial = first.get_question(qid)
        payload = _review_payload(initial, "stage9 disposable restore smoke")
        saved = first.save_review(qid, payload)
        if saved["version"] != initial["version"] + 1:
            raise RuntimeError("human review smoke did not append exactly one version")
        try:
            second.save_review(qid, payload)
        except Conflict:
            stale_conflict = True
        else:
            raise RuntimeError("stale human review unexpectedly overwrote central state")

        # Shared pair: mark 25/26 verified atomically, then prove stale pair
        # version cannot overwrite it.
        audio_qid = "035-I-L-025"
        audio = first.get_question(audio_qid)["audio_segment"]
        verify_payload = {
            "version": audio["version"],
            "start_ms": audio["start_ms"],
            "end_ms": audio["end_ms"],
            "status": "verified",
        }
        verified = first.save_audio_segment(audio_qid, verify_payload)
        if verified["audio_segment"]["status"] != "verified":
            raise RuntimeError("shared audio pair was not verified")
        try:
            second.save_audio_segment(audio_qid, verify_payload)
        except Conflict:
            audio_stale_conflict = True
        else:
            raise RuntimeError("stale shared-audio write unexpectedly succeeded")

        # First device creates canonical clip; second device initially has no
        # local file then rematerializes the exact central checksum without
        # appending duplicate export history.
        first.export_audio_clip(audio_qid)
        db = connect_postgres(url, readonly=True)
        try:
            canonical = db.execute(
                "SELECT clip_sha256 FROM audio_segments WHERE question_id=%s",
                (audio_qid,),
            ).fetchone()
            canonical_sha = canonical["clip_sha256"] if canonical else None
        finally:
            db.close()
        if not canonical_sha:
            raise RuntimeError("clip export did not establish canonical SHA")
        if second.get_question(audio_qid)["audio_segment"]["clip_url"] is not None:
            raise RuntimeError("second media root unexpectedly already had the canonical clip")
        db = connect_postgres(url, readonly=True)
        try:
            before_history = db.execute(
                "SELECT COUNT(*) AS count FROM review_records WHERE scope='audio_export_35'"
            ).fetchone()["count"]
        finally:
            db.close()
        second.export_audio_clip(audio_qid)
        db = connect_postgres(url, readonly=True)
        try:
            current = db.execute(
                "SELECT clip_sha256 FROM audio_segments WHERE question_id=%s",
                (audio_qid,),
            ).fetchone()
            current_sha = current["clip_sha256"] if current else None
        finally:
            db.close()
        if current_sha != canonical_sha:
            raise RuntimeError("rematerialized clip checksum differs from canonical SHA")
        db = connect_postgres(url, readonly=True)
        try:
            after_history = db.execute(
                "SELECT COUNT(*) AS count FROM review_records WHERE scope='audio_export_35'"
            ).fetchone()["count"]
        finally:
            db.close()
        if after_history != before_history:
            raise RuntimeError("local rematerialization duplicated central export history")

        # Stage-7 append-only AI audit: create one blind pass and one failed
        # attempt, then read it back through the public audit reader.
        run = ai_audit_35.create_run(
            url,
            auditors=["stage9-restore-smoke"],
            model_id="stage9-smoke",
            label="stage9 disposable restore smoke",
            subject_ids=[qid],
        )
        pass_id = run["passes"][0]["id"]
        attempt = ai_audit_35.record_attempt_outcome(
            url,
            pass_id,
            "failed",
            error_code="stage9_restore_smoke",
            error_message="expected disposable smoke outcome",
            evidence={"stage": 9, "purpose": "restore-read-write-smoke"},
        )
        attempts = ai_audit_35.list_attempts(url, pass_id=pass_id)
        if len(attempts) != 1 or attempts[0]["id"] != attempt["id"]:
            raise RuntimeError("AI audit write was not readable through append-only API")

        return {
            "status": "ok",
            "human_stale_conflict": stale_conflict,
            "shared_audio_stale_conflict": audio_stale_conflict,
            "clip_sha256": canonical_sha,
            "clip_rematerialized_without_duplicate_history": after_history == before_history,
            "ai_audit_attempt_readback": True,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run_smoke(args.database_url), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Stage 9 smoke blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
