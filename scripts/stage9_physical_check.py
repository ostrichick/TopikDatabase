"""Explicit, ordered HTTP checks on two physical clients and a disposable DB.

Run prepare on both devices, pc-write on PC, laptop-check on Laptop, and
pc-check on PC. Configuration and state live outside Git in --runtime.
Never accepts the operational database as a mutation target.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import socket
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.stage9_operational import verify_immutable_source
from scripts.stage9_smoke import _materialize_media_root, _review_payload
from src import ai_audit_35
from src.database import connect_postgres
from src.review_ui import ReviewStore, ThreadingHTTPServer, make_handler

QID = "035-I-R-031"
AUDIO = "035-I-L-025"
PAIR = "035-I-L-026"
TARGET = "topik_stage9_physical_20261007"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(action: str, runtime: Path) -> dict:
    runtime = runtime.resolve()
    config = json.loads((runtime / "connection.json").read_text(encoding="utf-8-sig"))
    url = config["database_url"]
    require(urlsplit(url).path == "/" + TARGET, "Only the disposable physical-check DB is allowed")
    identity = socket.gethostname().upper()
    expected_host = "DUBUYOGA" if action == "laptop-check" else "DUBUDESKTOP"
    if action != "prepare":
        require(identity == expected_host, "Action is running on the wrong physical device")
    source = verify_immutable_source()
    work = runtime / "physical-smoke"
    media = work / "media"
    state_path = work / "state.json"
    work.mkdir(parents=True, exist_ok=True)
    if not media.exists():
        media.mkdir()
        _materialize_media_root(media)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    store = ReviewStore(root=ROOT, database_url=url, media_root=media)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    def request(method, path, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=60)
        try:
            headers = {"Origin": origin}
            body = None
            if payload is not None:
                headers.update({"Content-Type": "application/json", "X-Review-Token": token})
                body = json.dumps(payload).encode("utf-8")
            conn.request(method, path, body, headers)
            response = conn.getresponse()
            data = response.read()
            if response.getheader("Content-Type", "").startswith("application/json"):
                data = json.loads(data)
            return response.status, data
        finally:
            conn.close()

    def get(qid):
        code, detail = request("GET", f"/api/questions/{qid}")
        require(code == 200, f"Detail GET failed: {code}")
        return detail

    def post(qid, endpoint, payload, expected=200):
        code, detail = request("POST", f"/api/questions/{qid}/{endpoint}", payload)
        require(code == expected, f"{endpoint} expected {expected}, received {code}")
        return detail

    def export_count():
        db = connect_postgres(url, readonly=True)
        try:
            return db.execute("SELECT count(*) AS n FROM review_records WHERE scope='audio_export_35'").fetchone()["n"]
        finally:
            db.close()

    def canonical_sha():
        db = connect_postgres(url, readonly=True)
        try:
            return db.execute("SELECT clip_sha256 FROM audio_segments WHERE question_id=%s", (AUDIO,)).fetchone()["clip_sha256"]
        finally:
            db.close()

    def append_audit(label):
        created = ai_audit_35.create_run(url, auditors=[label], model_id="stage9-physical-smoke",
                                          label=label, subject_ids=[QID])
        pid = created["passes"][0]["id"]
        ai_audit_35.record_attempt_outcome(url, pid, "failed", error_code="expected_physical_smoke",
            error_message="Disposable two-device check", evidence={"device": identity})
        return pid

    def audit_from_other(label):
        db = connect_postgres(url, readonly=True)
        try:
            rows = db.execute("SELECT p.id FROM ai_audit_passes p JOIN ai_audit_runs r ON r.id=p.run_id WHERE r.label=%s", (label,)).fetchall()
        finally:
            db.close()
        require(len(rows) == 1, "Other-device audit run missing or duplicated")
        attempts = ai_audit_35.list_attempts(url, pass_id=rows[0]["id"])
        require(len(attempts) == 1 and attempts[0]["error_code"] == "expected_physical_smoke", "Other-device audit readback failed")

    try:
        code, listing = request("GET", "/api/questions")
        require(code == 200 and listing["database_backend"] == "postgres", "Reviewer is not using PostgreSQL")
        token = listing["csrf_token"]
        if action == "prepare":
            require(not state, "Preparation already exists; do not silently reset evidence")
            state = {"device": identity, "question": get(QID), "audio": get(AUDIO)["audio_segment"]}
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            result = {"prepared": True, "question_version": state["question"]["version"], "audio_version": state["audio"]["version"]}
        elif action in ("pc-write", "pc-finish"):
            require(state.get("device") == identity, "Prepare this physical device first")
            if action == "pc-write":
                post(QID, "review", _review_payload(state["question"], "stage9 physical PC write"))
                audio = state["audio"]
                post(AUDIO, "audio-segment", {"version": audio["version"], "start_ms": audio["start_ms"], "end_ms": audio["end_ms"], "status": "verified"})
                post(AUDIO, "export-clip", {})
            else:
                require(get(QID)["version"] == state["question"]["version"] + 1 and export_count() == 2,
                        "Explicit resume requires exactly the previous PC writes and shared export")
            require(get(PAIR)["audio_segment"]["status"] == "verified", "Shared-pair write missing")
            clip = get(AUDIO)["audio_segment"]
            require(clip["clip_url"], "First canonical export missing")
            sha = canonical_sha()
            code, content = request("GET", clip["clip_url"])
            require(code == 200 and hashlib.sha256(content).hexdigest() == sha, "First clip HTTP bytes differ")
            pid = append_audit("stage9-physical-PC")
            result = {"human_write": True, "shared_audio_write": True, "clip_sha256": sha, "export_history_count": export_count(), "audit_pass": pid}
        elif action == "laptop-check":
            require(state.get("device") == identity, "Prepare this physical device first")
            current = get(QID)
            require(current["version"] == state["question"]["version"] + 1, "PC review is not visible on Laptop")
            post(QID, "review", _review_payload(state["question"], "stale Laptop write"), 409)
            post(QID, "review", _review_payload(current, "stage9 physical Laptop write"))
            audio = state["audio"]
            post(PAIR, "audio-segment", {"version": audio["version"], "start_ms": audio["start_ms"], "end_ms": audio["end_ms"], "status": "candidate"}, 409)
            clip = get(AUDIO)["audio_segment"]
            sha = canonical_sha()
            require(clip["status"] == "verified" and sha and clip["clip_url"] is None, "Laptop must see central metadata without a local clip")
            before = export_count()
            post(AUDIO, "export-clip", {})
            local = get(AUDIO)["audio_segment"]
            code, content = request("GET", local["clip_url"])
            require(code == 200 and hashlib.sha256(content).hexdigest() == sha, "Laptop clip HTTP bytes differ from PC")
            require(export_count() == before, "Rematerialization duplicated central history")
            audit_from_other("stage9-physical-PC")
            append_audit("stage9-physical-Laptop")
            result = {"pc_write_readback": True, "human_stale_http": 409, "shared_audio_stale_http": 409, "clip_sha256": sha, "no_duplicate_export_history": True, "pc_audit_readback": True, "laptop_review_and_audit_write": True}
        else:
            require(state.get("device") == identity, "Prepare this physical device first")
            require(get(QID)["version"] == state["question"]["version"] + 2, "Laptop review is not visible on PC")
            audit_from_other("stage9-physical-Laptop")
            result = {"laptop_review_readback": True, "laptop_audit_readback": True, "export_history_count": export_count()}
        require(verify_immutable_source() == source, "Original source changed during physical check")
        report = {"status": "PASS", "action": action, "device": identity, "source_unchanged": True, **result}
        (work / f"{action}-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "pc-write", "pc-finish", "laptop-check", "pc-check"))
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.action, args.runtime), indent=2))
