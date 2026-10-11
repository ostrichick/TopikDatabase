"""Read-only benchmarking instrumentation cannot modify review data."""

import unittest
from unittest.mock import patch

from scripts import benchmark_review_navigation as benchmark


class FakeStore:
    def __init__(self, exam_id):
        self.exam_id = exam_id

    def get_question(self, qid, *, fast=False):
        assert fast is True
        assert qid.startswith(self.exam_id[:3])
        return {"id": qid}

    def get_questions_bundle(self):
        return {"questions": {str(n): None for n in range(70)}}


class ReviewLatencyBenchmarkTests(unittest.TestCase):
    def test_percentiles_have_stable_nearest_rank_semantics(self):
        metrics = benchmark.percentiles([5.0, 1.0, 3.0, 2.0])
        self.assertEqual(metrics, {
            "n": 4, "min_ms": 1.0, "p50_ms": 2.5,
            "p95_ms": 5.0, "max_ms": 5.0,
        })

    def test_read_only_navigation_and_bundle_only(self):
        with patch.object(benchmark, "ReviewStore", FakeStore):
            result = benchmark.benchmark(repeat=2, warmup=1)
        self.assertEqual(result["DB_writes"], 0)
        self.assertEqual(result["purpose"], "read_only_navigation_baseline")
        self.assertEqual(set(result["metrics"]), {"035-I-B", "036-I-B"})
        for row in result["metrics"].values():
            self.assertEqual(row["fast_detail"]["n"], 2)
            self.assertEqual(row["bundle_70"]["n"], 2)
            self.assertGreaterEqual(row["store_init_ms"], 0)

    def test_reject_excessive_benchmark_traffic(self):
        with self.assertRaises(ValueError):
            benchmark.benchmark(repeat=100)
        with self.assertRaises(ValueError):
            benchmark.benchmark(repeat=1, warmup=100)


if __name__ == "__main__":
    unittest.main()
