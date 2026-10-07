"""Golden retrieval evaluation for MemBus.

The evaluator always uses an isolated temporary canonical store. Evaluation
therefore cannot pollute a user's real memory database.
"""

from __future__ import annotations

import json
import math
import statistics
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .db import Database
from .models import MemoryCreate, SearchContext, SearchRequest
from .services.memory_service import MemoryService


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    query_count: int
    recall_at_5: float
    recall_at_10: float
    mrr: float
    precision_at_5: float
    zero_result_rate: float
    wrong_scope_hit_rate: float
    latency_p50_ms: float
    latency_p95_ms: float
    failed_queries: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_dataset(path: str | Path) -> EvaluationReport:
    dataset_path = Path(path)
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="membus-eval-") as temp_dir:
        service = MemoryService(Database(Path(temp_dir) / "eval.db"))
        key_to_id = _seed(service, payload)

        recalls_5: list[float] = []
        recalls_10: list[float] = []
        reciprocal_ranks: list[float] = []
        precisions_5: list[float] = []
        zero_results = 0
        wrong_scope_hits = 0
        latencies: list[float] = []
        failed: list[str] = []

        queries = payload.get("queries", [])
        for case in queries:
            query_name = str(case.get("name") or case["query"])
            context = SearchContext(**case.get("context", {}))
            expected_keys = tuple(case.get("expected", ()))
            forbidden_keys = tuple(case.get("forbidden", ()))
            expected_ids = {key_to_id[key] for key in expected_keys}
            forbidden_ids = {key_to_id[key] for key in forbidden_keys}

            started = time.perf_counter()
            results = service.search(
                SearchRequest.with_types(
                    query=str(case["query"]),
                    context=context,
                    types=case.get("types", ()),
                    limit=10,
                )
            )
            latencies.append((time.perf_counter() - started) * 1000.0)

            result_ids = [result.memory.id for result in results]
            if not result_ids:
                zero_results += 1

            top5 = result_ids[:5]
            top10 = result_ids[:10]
            expected_count = max(1, len(expected_ids))

            recalls_5.append(len(expected_ids.intersection(top5)) / expected_count)
            recalls_10.append(len(expected_ids.intersection(top10)) / expected_count)
            precisions_5.append(len(expected_ids.intersection(top5)) / 5.0)

            first_rank = next(
                (
                    index
                    for index, memory_id in enumerate(result_ids, start=1)
                    if memory_id in expected_ids
                ),
                None,
            )
            reciprocal_ranks.append(1.0 / first_rank if first_rank is not None else 0.0)

            if forbidden_ids.intersection(top10):
                wrong_scope_hits += 1

            if not expected_ids.intersection(top10):
                failed.append(query_name)

        count = len(queries)
        if count == 0:
            raise ValueError("Golden dataset must contain at least one query")

        return EvaluationReport(
            query_count=count,
            recall_at_5=statistics.fmean(recalls_5),
            recall_at_10=statistics.fmean(recalls_10),
            mrr=statistics.fmean(reciprocal_ranks),
            precision_at_5=statistics.fmean(precisions_5),
            zero_result_rate=zero_results / count,
            wrong_scope_hit_rate=wrong_scope_hits / count,
            latency_p50_ms=_percentile(latencies, 50),
            latency_p95_ms=_percentile(latencies, 95),
            failed_queries=tuple(failed),
        )


def _seed(service: MemoryService, payload: dict[str, Any]) -> dict[str, str]:
    for alias in payload.get("aliases", []):
        service.put_alias(str(alias["alias"]), str(alias["canonical"]))

    key_to_id: dict[str, str] = {}
    for entry in payload.get("memories", []):
        key = str(entry["key"])
        if key in key_to_id:
            raise ValueError(f"Duplicate golden memory key: {key}")

        memory_payload = dict(entry["memory"])
        memory = service.put(MemoryCreate(**memory_payload), actor="golden-eval")
        key_to_id[key] = memory.id

    return key_to_id


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    if not 0 <= percentile <= 100:
        raise ValueError("percentile must be in [0, 100]")

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]

    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction
