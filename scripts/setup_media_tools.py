"""Install and verify the exact local FFmpeg build used for canonical clips.

Run this once on every Windows PC/Laptop that exports TOPIK audio clips:

    py -3 scripts/setup_media_tools.py

The binary stays under the Git-ignored local corpus dependency directory. The
script never modifies source PDF/MP3 files or the pilot SQLite database.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPS = ROOT / "topik-past-papers" / ".verification_deps"
PACKAGE = "imageio-ffmpeg==0.6.0"


def install(*, repair: bool = False) -> None:
    DEPS.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
        "--target", str(DEPS), "--no-deps",
    ]
    if repair:
        command.append("--upgrade")
    command.append(PACKAGE)
    response = subprocess.run(command, cwd=ROOT, check=False)
    if response.returncode:
        raise RuntimeError(f"Failed to install {PACKAGE} (exit {response.returncode})")


def verify() -> dict[str, str]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from src.audio_35 import pinned_ffmpeg_identity

    pinned_ffmpeg_identity.cache_clear()
    identity = pinned_ffmpeg_identity()
    return {
        "status": "ready",
        "package": PACKAGE,
        "ffmpeg_version": identity["version"],
        "ffmpeg_sha256": identity["sha256"],
        "ffmpeg_path": identity["path"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repair", action="store_true",
        help="Reinstall/upgrade the pinned wheel before verifying the approved binary",
    )
    parser.add_argument(
        "--verify-only", action="store_true",
        help="Do not call pip; only validate the already-installed pinned binary",
    )
    args = parser.parse_args(argv)
    try:
        if args.verify_only:
            result = verify()
        elif args.repair:
            install(repair=True)
            result = verify()
        else:
            try:
                # The common second-device/re-run case should be offline and
                # instant when the exact approved binary is already present.
                result = verify()
            except (OSError, RuntimeError, ValueError):
                # Missing or invalid local bytes require a forced replacement;
                # a plain --target install would leave an existing bad package.
                install(repair=True)
                result = verify()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Pinned FFmpeg setup blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
