"""Read-only TOPIK reviewer navigation benchmark; never submits review writes.

Run with the existing verified TOPIK_DATABASE_URL and TOPIK_MEDIA_ROOT. This
calls only reviewer GET read paths. Timings contain no credentials, exam text,
reviewer evidence, local media paths, or approval payloads.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.review_ui import ReviewStore


def percentiles(samples: list[float]) -> dict:
    values = sorted(samples)
    if not values:
        return {"n": 0}
    # Nearest-rank p95 is meaningful even for short local diagnostic samples.
    index_95 = max(0, (95 * len(values) + 99) // 100 - 1)
    return {"n": len(values), "min_ms": round(values[0], 1),
            "p50_ms": round(statistics.median(values), 1),
            "p95_ms": round(values[index_95], 1),
            "max_ms": round(values[-1], 1)}


def benchmark(*, repeat: int = 3, warmup: int = 0) -> dict:
    if not 1 <= repeat <= 25 or not 0 <= warmup <= 5:
        raise ValueError("repeat must be 1..25 and warmup 0..5")
    output: dict = {"purpose": "read_only_navigation_baseline",
                    "DB_writes": 0, "metrics": {}}
    for exam in ("035-I-B", "036-I-B"):
        started = time.perf_counter()
        store = ReviewStore(exam_id=exam)
        start_ms = (time.perf_counter() - started) * 1000
        ids = (f"{exam[:3]}-I-L-001", f"{exam[:3]}-I-L-010",
               f"{exam[:3]}-I-R-035")
        metrics: dict = {"store_init_ms": round(start_ms, 1)}
        for label, invoke in (
            ("fast_detail", lambda n: store.get_question(ids[n % len(ids)], fast=True)),
            ("bundle_70", lambda _n: store.get_questions_bundle()),
        ):
            times = []
            for n in range(repeat + warmup):
                begin = time.perf_counter()
                data = invoke(n)
                duration = (time.perf_counter() - begin) * 1000
                if label == "fast_detail" and data.get("id") not in ids:
                    raise AssertionError("Unexpected exam detail")
                if label == "bundle_70" and len(data["questions"]) != 70:
                    raise AssertionError("Unexpected exam bundle")
                if n >= warmup:
                    times.append(duration)
            metrics[label] = percentiles(times)
        output["metrics"][exam] = metrics
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--json", type=Path, default=None,
                        help="Optional private/ignored destination for metrics only")
    args = parser.parse_args()
    report = benchmark(repeat=args.repeat, warmup=args.warmup)
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
