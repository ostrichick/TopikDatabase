"""Conservative, local-only audio proposals for the 35th TOPIK I listening test.

The long pauses in this *particular recording* strongly suggest an example followed
by 30 numbered response windows. They do NOT prove question/audio alignment. All
returned times are candidates for human review, never approved timestamps.

From the repository root:
    py -3 src/audio_35.py analyze
    py -3 src/audio_35.py clip --start-ms 84500 --end-ms 119900 --output topik-past-papers/derived/audio_35/sample.mp3

The clip command needs an existing destination directory, never overwrites a file,
and refuses to write into the original 35th-session source directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "topik-past-papers" / "35th"
DEFAULT_SOURCE = SOURCE_DIR / "35-TOPIK-I-Listening-Audio-File.mp3"
SHARED_PAIRS = ((25, 26), (27, 28), (29, 30))
_THRESHOLDS = (-30, -35, -40, -45)
_LONG_SILENCE_SECONDS = 8.0
_SILENCE_LINE = re.compile(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)\s*\|\s*silence_duration:\s*([0-9]+(?:\.[0-9]+)?)")
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):([0-9]+(?:\.[0-9]+)?)")


class AudioError(ValueError):
    """Missing utility, unsafe output, unsupported media, or uncertain analysis."""


def _source(path: str | Path) -> Path:
    candidate = Path(path).resolve(strict=True)
    if not candidate.is_file() or candidate.suffix.lower() != ".mp3":
        raise AudioError("A readable MP3 source file is required")
    with candidate.open("rb") as stream:
        header = stream.read(10)
    if not (header.startswith(b"ID3") or len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0):
        raise AudioError("Source has no recognizable MP3 header")
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ffmpeg(explicit: str | Path | None = None) -> str:
    if explicit is not None:
        path = shutil.which(str(explicit))
        if path is None:
            raise AudioError(f"ffmpeg executable not found: {explicit}")
        return path
    located = shutil.which("ffmpeg")
    if located:
        return located
    # The local corpus already has imageio_ffmpeg in its ignored verification deps.
    try:
        import imageio_ffmpeg
    except ImportError:
        sys.path.insert(0, str(ROOT / "topik-past-papers" / ".verification_deps"))
        try:
            import imageio_ffmpeg
        except ImportError as exc:
            raise AudioError("Install ffmpeg or imageio_ffmpeg locally, or pass --ffmpeg") from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"Local ffmpeg failed: {type(exc).__name__}") from exc


def probe_audio(source: str | Path = DEFAULT_SOURCE, *, ffmpeg: str | Path | None = None) -> int:
    """Return the source duration in milliseconds, without altering the file."""
    path = _source(source)
    # Mutagen reads VBR/Xing frame counts more precisely than ffmpeg's rounded
    # header duration. It is optional when using this helper in another project.
    try:
        from mutagen.mp3 import MP3
    except ImportError:
        sys.path.insert(0, str(ROOT / "topik-past-papers" / ".verification_deps"))
        try:
            from mutagen.mp3 import MP3
        except ImportError:
            MP3 = None
    if MP3 is not None:
        try:
            duration = MP3(path).info.length
            if 0 < duration < 24 * 3600:
                return round(duration * 1000)
        except (ValueError, OSError):
            pass
    output = _run([_ffmpeg(ffmpeg), "-hide_banner", "-nostdin", "-i", str(path)])
    match = _DURATION.search(output.stderr)
    if not match:
        raise AudioError("Cannot determine duration from this MP3")
    hours, minutes, seconds = match.groups()
    duration_ms = round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)
    if not 0 < duration_ms < 24 * 3600 * 1000:
        raise AudioError("Invalid audio duration")
    return duration_ms


def _long_pauses(source: Path, ffmpeg: str, noise_db: int) -> list[tuple[int, int]]:
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info", "-i", str(source),
               "-vn", "-af", f"silencedetect=noise={noise_db}dB:d=0.6", "-f", "null", "-"]
    response = _run(command)
    if response.returncode:
        raise AudioError(f"ffmpeg silence analysis failed (exit {response.returncode})")
    pauses = []
    for end, duration in _SILENCE_LINE.findall(response.stderr):
        end, duration = float(end), float(duration)
        if duration >= _LONG_SILENCE_SECONDS:
            pauses.append((round((end - duration) * 1000), round(end * 1000)))
    return pauses


def _propose(pauses: list[tuple[int, int]], duration_ms: int, sha256: str) -> list[dict]:
    """Fail closed unless 31 ordered, long response pauses support 30 intervals."""
    if len(pauses) != 31:
        return []
    if any(start < 0 or end > duration_ms + 150 or end - start < 8000
           for start, end in pauses):
        return []
    if any(pauses[index][1] >= pauses[index + 1][0] for index in range(30)):
        return []

    windows = [(pauses[index - 1][1], pauses[index][0]) for index in range(1, 31)]
    if any(not 500 <= end - start <= 150000 for start, end in windows):
        return []
    # The 26/28/30 windows contain only about one second of sound after a long
    # dialogue in 25/27/29. Never publish these as independent full passages.
    for first, second in SHARED_PAIRS:
        a, b = windows[first - 1], windows[second - 1]
        if not (a[1] - a[0] > 60000 and 500 <= b[1] - b[0] <= 3500):
            return []

    candidates = []
    groups = [(number,) for number in range(1, 25)] + list(SHARED_PAIRS)
    for questions in groups:
        begin = windows[questions[0] - 1][0]
        finish = windows[questions[-1] - 1][1]
        candidates.append({
            "questions": list(questions),
            "start_ms": max(0, begin - 150),
            "end_ms": min(duration_ms, finish + 150),
            "source_sha256": sha256,
            "status": "candidate_unverified",
            "method": "31_stable_long_pauses_example_plus_30_hypothesis",
            "contains_response_pause": len(questions) > 1,
            "requires_human_audio_review": True,
        })
    return candidates


def analyze_audio(source: str | Path = DEFAULT_SOURCE, *, ffmpeg: str | Path | None = None) -> dict:
    """Return evidence and tentative candidates, never assert alignment."""
    path = _source(source)
    binary = _ffmpeg(ffmpeg)
    duration_ms = probe_audio(path, ffmpeg=binary)
    sha256 = _sha256(path)
    observed = {str(level): _long_pauses(path, binary, level) for level in _THRESHOLDS}
    primary = observed["-35"]
    stable = all(len(pauses) == len(primary) == 31 and all(
        abs(left[0] - right[0]) <= 120 and abs(left[1] - right[1]) <= 120
        for left, right in zip(pauses, primary)) for pauses in observed.values())
    candidates = _propose(primary, duration_ms, sha256) if stable else []
    return {
        "source": str(path), "source_sha256": sha256, "duration_ms": duration_ms,
        "silence_thresholds_db": list(_THRESHOLDS),
        "long_silence_min_ms": round(_LONG_SILENCE_SECONDS * 1000),
        "long_pause_counts": {key: len(value) for key, value in observed.items()},
        "long_pause_intervals_ms": [{"start_ms": a, "end_ms": b} for a, b in primary],
        "thresholds_stable": stable,
        "candidate_count": len(candidates), "candidates": candidates,
        "limitations": [
            "Ordinal mapping assumes first long pause follows the example; not independently aligned to voice/transcript.",
            "A threshold-stable pause pattern is NOT proof of correct question boundaries.",
            "Shared 25/26, 27/28, 29/30 clips span a long response silence so the full dialogue is retained.",
            "Listen to every candidate and verify its boundaries before assigning, storing or distributing clips.",
        ],
    }


def detect_candidate_boundaries(source: str | Path = DEFAULT_SOURCE,
                                *, ffmpeg: str | Path | None = None) -> list[dict]:
    """Return <=27 provisional clips covering 30 questions; [] if ambiguous."""
    return analyze_audio(source, ffmpeg=ffmpeg)["candidates"]


def export_segment(source: str | Path, dest: str | Path, start_ms: int, end_ms: int,
                   *, ffmpeg: str | Path | None = None,
                   expected_source_sha256: str | None = None) -> Path:
    """Decode/encode a bounded clip atomically; source stays untouched.

    Only MP3 output is supported. Exact audio timing depends on encoder delay,
    and the caller must manually validate the exported clip by listening.
    """
    path = _source(source)
    target = Path(dest).resolve()
    if target.suffix.lower() != ".mp3":
        raise AudioError("Output must be an .mp3 file")
    if not target.parent.is_dir():
        raise AudioError("Create the output directory explicitly before clipping")
    if target.exists() or target == path:
        raise AudioError("Refusing to overwrite an existing file or source")
    if target.is_relative_to(SOURCE_DIR.resolve()):
        raise AudioError("Refusing to write clips into the original source corpus")
    if type(start_ms) is not int or type(end_ms) is not int:
        raise AudioError("Clip boundaries must be integer milliseconds")
    duration = probe_audio(path, ffmpeg=ffmpeg)
    if not 0 <= start_ms < end_ms <= duration or end_ms - start_ms < 500:
        raise AudioError("Invalid clip boundaries")
    if expected_source_sha256 is not None and _sha256(path) != expected_source_sha256:
        raise AudioError("Source SHA-256 changed since candidate detection")

    handle, temp_name = tempfile.mkstemp(prefix=".audio35-", suffix=".part", dir=target.parent)
    os.close(handle)
    temp = Path(temp_name)
    try:
        args = [_ffmpeg(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                "-i", str(path), "-ss", f"{start_ms / 1000:.3f}", "-t",
                f"{(end_ms - start_ms) / 1000:.3f}", "-map", "0:a:0", "-vn", "-sn",
                "-dn", "-c:a", "libmp3lame", "-b:a", "160k", "-f", "mp3", str(temp)]
        response = _run(args)
        if response.returncode or temp.stat().st_size < 128:
            raise AudioError(f"ffmpeg clip export failed (exit {response.returncode})")
        # Hard-link only if the final name does not exist; os.replace would
        # silently overwrite a clip created by another process in the meantime.
        os.link(temp, target)
        return target
    except FileExistsError as exc:
        raise AudioError("Destination appeared while exporting; not overwritten") from exc
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=("analyze", "clip"))
    cli.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    cli.add_argument("--ffmpeg", default=None, help="Optional local ffmpeg executable")
    cli.add_argument("--output", type=Path)
    cli.add_argument("--start-ms", type=int)
    cli.add_argument("--end-ms", type=int)
    cli.add_argument("--expected-source-sha256")
    args = cli.parse_args(argv)
    try:
        if args.command == "analyze":
            print(json.dumps(analyze_audio(args.source, ffmpeg=args.ffmpeg),
                             ensure_ascii=False, indent=2))
        else:
            if args.output is None or args.start_ms is None or args.end_ms is None:
                cli.error("clip requires --output, --start-ms and --end-ms")
            exported = export_segment(args.source, args.output, args.start_ms, args.end_ms,
                                      ffmpeg=args.ffmpeg,
                                      expected_source_sha256=args.expected_source_sha256)
            print(f"Candidate clip exported; manually verify audio: {exported}")
        return 0
    except (AudioError, OSError) as exc:
        print(f"Audio analysis failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
