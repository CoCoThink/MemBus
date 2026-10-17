"""Golden retrieval evaluation for repository/CI use."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from membus import MemBus


def evaluate_dataset(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="membus-eval-") as temp_dir:
        bus = MemBus(Path(temp_dir) / "eval.db")
        key_to_id = _seed(bus, payload)

        recalls_5: list[float] = []
        recalls_10: list[float] = []
        reciprocal_ranks: list[float] = []
        latencies: list[float] = []
        failed: list[str] = []
        zero_results = 0
        wrong_context_hits = 0

        queries = payload.get("queries", [])
        if not queries:
            raise ValueError("Golden dataset must contain at least one query")

        for case in queries:
            name = str(case.get("name") or case["query"])
            expected_ids = {
                key_to_id[key] for key in case.get("expected", ())
            }
            forbidden_ids = {
                key_to_id[key] for key in case.get("forbidden", ())
            }

            started = time.perf_counter()
            results = bus.search(
                str(case["query"]),
                **case.get("context", {}),
                types=case.get("types", ()),
                limit=10,
            )
            latencies.append((time.perf_counter() - started) * 1000.0)

            result_ids = [memory.id for memory in results]
            if not result_ids:
                zero_results += 1

            expected_count = max(1, len(expected_ids))
            recalls_5.append(
                len(expected_ids.intersection(result_ids[:5])) / expected_count
            )
            recalls_10.append(
                len(expected_ids.intersection(result_ids[:10])) / expected_count
            )

            first_rank = next(
                (
                    index
                    for index, memory_id in enumerate(result_ids, start=1)
                    if memory_id in expected_ids
                ),
                None,
            )
            reciprocal_ranks.append(
                1.0 / first_rank if first_rank is not None else 0.0
            )

            if forbidden_ids.intersection(result_ids[:10]):
                wrong_context_hits += 1
            if not expected_ids.intersection(result_ids[:10]):
                failed.append(name)

        count = len(queries)
        return {
            "query_count": count,
            "recall_at_5": statistics.fmean(recalls_5),
            "recall_at_10": statistics.fmean(recalls_10),
            "mrr": statistics.fmean(reciprocal_ranks),
            "zero_result_rate": zero_results / count,
            "wrong_context_hit_rate": wrong_context_hits / count,
            "latency_p50_ms": _percentile(latencies, 50),
            "latency_p95_ms": _percentile(latencies, 95),
            "failed_queries": failed,
        }


def _seed(bus: MemBus, payload: dict[str, Any]) -> dict[str, str]:
    for alias in payload.get("aliases", []):
        bus.put_alias(str(alias["alias"]), str(alias["canonical"]))

    key_to_id: dict[str, str] = {}
    for entry in payload.get("memories", []):
        key = str(entry["key"])
        if key in key_to_id:
            raise ValueError(f"Duplicate golden memory key: {key}")
        key_to_id[key] = bus.put(**entry["memory"]).id
    return key_to_id


def _percentile(values: list[float], percentile: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]

    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset")
    parser.add_argument("--min-recall-5", type=float)
    parser.add_argument("--min-recall-10", type=float)
    parser.add_argument("--max-wrong-context-rate", type=float)
    args = parser.parse_args(argv)

    report = evaluate_dataset(args.dataset)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))

    failures: list[str] = []
    if args.min_recall_5 is not None and report["recall_at_5"] < args.min_recall_5:
        failures.append(
            f"recall@5 {report['recall_at_5']:.4f} < {args.min_recall_5:.4f}"
        )
    if (
        args.min_recall_10 is not None
        and report["recall_at_10"] < args.min_recall_10
    ):
        failures.append(
            f"recall@10 {report['recall_at_10']:.4f} < {args.min_recall_10:.4f}"
        )
    if (
        args.max_wrong_context_rate is not None
        and report["wrong_context_hit_rate"] > args.max_wrong_context_rate
    ):
        failures.append(
            "wrong-context rate "
            f"{report['wrong_context_hit_rate']:.4f} > "
            f"{args.max_wrong_context_rate:.4f}"
        )

    for failure in failures:
        print(f"evaluation gate failed: {failure}", file=sys.stderr)
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
