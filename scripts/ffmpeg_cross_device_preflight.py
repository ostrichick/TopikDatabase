"""Prove that two devices produce byte-identical canonical MP3 clips.

On each device, create a local report with the same command. Then place the
other device's report on this device and pass it with ``--compare-report``.
No source media or SQLite data is modified; the test clip lives only in a
temporary directory and is deleted automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.audio_35 import DEFAULT_SOURCE, export_segment, pinned_ffmpeg_identity


EXPECTED_SOURCE_SHA256 = "314af506159e24b7bd4b20e95d248683673e799e8e8af5ac4b77946879e4dae2"
START_MS = 84_408
END_MS = 120_023
COMPARE_FIELDS = (
    "source_sha256", "start_ms", "end_ms", "ffmpeg_version",
    "ffmpeg_sha256", "clip_sha256",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_report(label: str) -> dict:
    source = DEFAULT_SOURCE.resolve(strict=True)
    source_sha = file_sha256(source)
    if source_sha != EXPECTED_SOURCE_SHA256:
        raise RuntimeError("Local source MP3 does not match the approved 35-I recording")
    ffmpeg = pinned_ffmpeg_identity()
    with tempfile.TemporaryDirectory(prefix="topik-ffmpeg-preflight-") as temporary:
        clip = Path(temporary) / "preflight.mp3"
        export_segment(
            source, clip, START_MS, END_MS,
            expected_source_sha256=EXPECTED_SOURCE_SHA256,
        )
        clip_sha = file_sha256(clip)
        clip_size = clip.stat().st_size
    return {
        "schema": "topik-ffmpeg-cross-device-preflight-v1",
        "label": label,
        "source_sha256": source_sha,
        "start_ms": START_MS,
        "end_ms": END_MS,
        "ffmpeg_version": ffmpeg["version"],
        "ffmpeg_sha256": ffmpeg["sha256"],
        "clip_sha256": clip_sha,
        "clip_byte_size": clip_size,
    }


def compare(local: dict, other: dict) -> list[str]:
    if other.get("schema") != local["schema"]:
        return ["schema"]
    return [field for field in COMPARE_FIELDS if other.get(field) != local.get(field)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="device", help="Human-readable device name, e.g. PC or Laptop")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    parser.add_argument("--compare-report", type=Path, help="JSON report produced on the other device")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.label)
        if args.output:
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if args.compare_report:
            other = json.loads(args.compare_report.expanduser().resolve().read_text(encoding="utf-8"))
            mismatches = compare(report, other)
            report["comparison"] = {
                "other_label": other.get("label"),
                "status": "PASS" if not mismatches else "FAIL",
                "mismatched_fields": mismatches,
            }
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if not mismatches else 2
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"FFmpeg cross-device preflight blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
