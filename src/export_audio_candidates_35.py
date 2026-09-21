"""Write 27 *unverified* preview MP3s for 35th TOPIK I to ignored local files.

The source recording and DB remain untouched. These are only approximate cuts;
use the review UI to listen, correct boundaries, verify, and export final clips.
Run after `py -3 src/prepare_audio_35.py` from the project root.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import audio_35, pilot_35
from src.prepare_audio_35 import REPORT_PATH, VERSION

OUTPUT = ROOT / "topik-past-papers" / "derived" / "audio-candidates"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(part)
    return result.hexdigest()


def export_candidates(db_path: Path = pilot_35.DB_PATH, report_path: Path = REPORT_PATH,
                      output_dir: Path = OUTPUT) -> dict:
    """Fail closed on edited/provenance mismatches, and never overwrite a clip."""
    db_path, report_path, output_dir = map(lambda path: Path(path).resolve(),
                                           (db_path, report_path, output_dir))
    data = json.loads(report_path.read_text(encoding="utf-8"))
    source = audio_35.DEFAULT_SOURCE.resolve()
    if data.get("source_sha256") != digest(source) or data.get("duration_ms") != audio_35.probe_audio(source):
        raise RuntimeError("Candidate analysis source no longer matches the original recording")
    candidates = data.get("candidates", [])
    if (len(candidates) != 27 or [q for row in candidates for q in row["questions"]] != list(range(1, 31))
            or any(row["status"] != "candidate_unverified" for row in candidates)):
        raise RuntimeError("Expected exactly 27 unverified proposals covering questions 1-30")
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
        metadata = dict(db.execute("SELECT key,value FROM import_metadata"))
        if metadata.get("audio_detection_version") != VERSION or metadata.get(
                "audio_detection_source_sha256") != data["source_sha256"]:
            raise RuntimeError("Audio detection has not been registered in this SQLite database")
        for row in candidates:
            for number in row["questions"]:
                stored = db.execute("SELECT start_ms,end_ms,status,source_sha256 FROM audio_segments "
                                    "WHERE question_id=?", (f"035-I-L-{number:03d}",)).fetchone()
                if stored != (row["start_ms"], row["end_ms"], "candidate", data["source_sha256"]):
                    raise RuntimeError(f"Question {number}: candidate was already changed; refusing stale export")

    if output_dir.is_relative_to(audio_35.SOURCE_DIR.resolve()):
        raise RuntimeError("Candidate output must not be placed in the immutable source folder")
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_manifest = output_dir / "manifest.json"
    if existing_manifest.exists():
        previous = json.loads(existing_manifest.read_text(encoding="utf-8"))
        if (previous.get("source_sha256") != data["source_sha256"] or
                len(previous.get("files", [])) != 27 or any(
                not (output_dir / item["name"]).is_file() or
                digest(output_dir / item["name"]) != item["sha256"] for item in previous["files"])):
            raise RuntimeError("Existing candidate manifest or exported files do not match; refusing overwrite")
        return {"already_exported": True, "clips": 27, "path": str(output_dir),
                "all_unverified": True}
    if list(output_dir.glob("*.mp3")):
        raise RuntimeError("Untracked MP3s already exist in the destination; refusing overwrite")

    made = []
    records = []
    try:
        for candidate in candidates:
            questions = candidate["questions"]
            suffix = "-".join(f"{number:03d}" for number in questions)
            name = f"035-I-L-{suffix}-CANDIDATE-UNVERIFIED.mp3"
            path = output_dir / name
            audio_35.export_segment(source, path, candidate["start_ms"], candidate["end_ms"],
                                    expected_source_sha256=data["source_sha256"])
            made.append(path)
            records.append({"name": name, "questions": questions,
                            "start_ms": candidate["start_ms"], "end_ms": candidate["end_ms"],
                            "status": "candidate_unverified", "sha256": digest(path),
                            "size_bytes": path.stat().st_size,
                            "actual_duration_ms": audio_35.probe_audio(path)})
        manifest = {"source_sha256": data["source_sha256"], "source_duration_ms": data["duration_ms"],
                    "warning": "Unverified silence-based proposals. Listen to each clip and confirm question and boundaries.",
                    "files": records}
        descriptor, tmp = tempfile.mkstemp(prefix=".candidate-manifest-", suffix=".part", dir=output_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(tmp, existing_manifest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except BaseException:
        # Remove only files generated in this invocation, never preexisting user assets.
        for path in made:
            path.unlink(missing_ok=True)
        raise
    if digest(source) != data["source_sha256"]:
        raise RuntimeError("Source recording changed during export; do not use candidate files")
    return {"already_exported": False, "clips": len(records), "covered_questions": 30,
            "path": str(output_dir), "manifest": str(existing_manifest), "all_unverified": True}


if __name__ == "__main__":
    try:
        print(json.dumps(export_candidates(), ensure_ascii=False, indent=2))
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
        print(f"Audio candidate export blocked: {error}", file=sys.stderr)
        raise SystemExit(1) from error
