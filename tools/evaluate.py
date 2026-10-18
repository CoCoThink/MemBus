"""Golden retrieval evaluation for repository/CI use."""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from membus import MemBus, MemoryRef
from membus.core import _content_hash, _normalize_memory_values


def evaluate_dataset(path: str | Path, scale: int = 0) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="membus-eval-") as temp_dir:
        bus = MemBus(Path(temp_dir) / "eval.db")
        key_to_id, failed_checks = _seed(bus, payload)
        if scale:
            _seed_synthetic(bus, scale)

        recalls_5: list[float] = []
        recalls_10: list[float] = []
        reciprocal_ranks: list[float] = []
        latencies: list[float] = []
        failed: list[str] = []
        zero_results = 0
        preference_errors = 0
        preference_cases = 0
        leak_cases = 0
        leaks: list[str] = []

        queries = payload.get("queries", [])
        if not queries:
            raise ValueError("Golden dataset must contain at least one query")

        for case in queries:
            name = str(case.get("name") or case["query"])
            expected_ids = {key_to_id[key] for key in case.get("expected", ())}
            lower_priority_ids = {
                key_to_id[key] for key in case.get("lower_priority", ())
            }
            absent_ids = {key_to_id[key] for key in case.get("absent", ())}

            started = time.perf_counter()
            results = bus.search(
                str(case["query"]),
                **case.get("context", {}),
                types=case.get("types", ()),
                limit=10,
                strict=bool(case.get("strict", False)),
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

            first_expected_rank = _first_rank(result_ids, expected_ids)
            reciprocal_ranks.append(
                1.0 / first_expected_rank if first_expected_rank is not None else 0.0
            )

            if lower_priority_ids:
                preference_cases += 1
                first_lower_rank = _first_rank(result_ids, lower_priority_ids)
                if first_lower_rank is not None and (
                    first_expected_rank is None
                    or first_lower_rank < first_expected_rank
                ):
                    preference_errors += 1

            if absent_ids:
                leak_cases += 1
                if absent_ids.intersection(result_ids):
                    leaks.append(name)

            if not expected_ids.intersection(result_ids[:10]):
                failed.append(name)

        # Scale runs only measure latency/noise: synthetic memories are newer
        # than the fixtures and would crowd them out of list pages.
        list_failures = [] if scale else _run_list_cases(bus, payload, key_to_id)
        count = len(queries)
        report: dict[str, Any] = {
            "query_count": count,
            "recall_at_5": statistics.fmean(recalls_5),
            "recall_at_10": statistics.fmean(recalls_10),
            "mrr": statistics.fmean(reciprocal_ranks),
            "zero_result_rate": zero_results / count,
            "context_preference_case_count": preference_cases,
            "context_preference_error_rate": (
                preference_errors / preference_cases if preference_cases else 0.0
            ),
            "superseded_leak_case_count": leak_cases,
            "superseded_leak_rate": len(leaks) / leak_cases if leak_cases else 0.0,
            "superseded_leaks": leaks,
            "list_case_count": 0 if scale else len(payload.get("list_cases", [])),
            "failed_checks": failed_checks + list_failures,
            "latency_p50_ms": _percentile(latencies, 50),
            "latency_p95_ms": _percentile(latencies, 95),
            "failed_queries": failed,
        }
        if scale:
            report["synthetic_memories"] = scale
        return report


def _first_rank(result_ids: list[str], wanted: set[str]) -> int | None:
    return next(
        (i for i, memory_id in enumerate(result_ids, start=1) if memory_id in wanted),
        None,
    )


def _seed(bus: MemBus, payload: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    for alias in payload.get("aliases", []):
        bus.put_alias(str(alias["alias"]), str(alias["canonical"]))

    key_to_id: dict[str, str] = {}
    failed_checks: list[str] = []
    sources: dict[str, dict[str, Any]] = {}
    for entry in payload.get("memories", []):
        key = str(entry["key"])
        if key in key_to_id:
            raise ValueError(f"Duplicate golden memory key: {key}")

        if "same_as" in entry:
            # Re-putting identical content must converge on the same memory.
            source_key = str(entry["same_as"])
            again = bus.put(**sources[source_key])
            key_to_id[key] = again.memory.id
            if again.created or again.memory.id != key_to_id[source_key]:
                failed_checks.append(f"dedupe:{key}")
            continue

        sources[key] = entry["memory"]
        supersedes = [
            MemoryRef(key_to_id[k], bus.get(key_to_id[k]).revision)
            for k in entry.get("supersedes", ())
        ]
        key_to_id[key] = bus.put(**entry["memory"], supersedes=supersedes).memory.id
    return key_to_id, failed_checks


def _run_list_cases(
    bus: MemBus, payload: dict[str, Any], key_to_id: dict[str, str],
) -> list[str]:
    failures: list[str] = []
    for case in payload.get("list_cases", []):
        page = bus.list(
            **case.get("context", {}),
            types=case.get("types", ()),
            include_inactive=bool(case.get("include_inactive", False)),
            limit=200,
        )
        got = {memory.id for memory in page.memories}
        expected = {key_to_id[key] for key in case.get("expected", ())}
        absent = {key_to_id[key] for key in case.get("absent", ())}
        if not expected <= got or got & absent:
            failures.append(f"list:{case['name']}")
    return failures


_TOPICS = (
    "database", "cache", "queue", "deployment", "migration", "timeout",
    "retry", "logging", "auth", "billing", "report", "dashboard", "backup",
    "monitoring", "scheduler", "gateway", "storage", "index", "pipeline",
    "release", "alert", "quota", "session", "export", "import", "webhook",
)
_VERBS = (
    "must", "should", "never", "always", "often", "rarely", "needs to",
    "is expected to",
)
_ACTIONS = (
    "be reviewed", "run in production", "use the shared config", "fail fast",
    "be rolled back", "page the on-call", "write an audit entry",
    "skip the staging step", "wait for the lock", "emit a metric",
)
_PROJECTS = (
    "billing", "search", "inventory", "identity", "mobile", "ads",
    "payment", "analytics", "support", "growth", "infra", "docs",
)
_ZH_NOUNS = ("服务", "客户端", "数据库", "缓存", "队列", "部署", "迁移", "日志", "监控", "报表")
_ZH_TEMPLATES = (
    "{a}需要在{b}完成之后才能更新。",
    "如何处理{a}和{b}之间的超时问题。",
    "{a}的{b}规则只适用于内部平台。",
)


def _seed_synthetic(bus: MemBus, count: int) -> None:
    """Bulk-insert noisy memories newer than the fixtures (the harsh case)."""
    rng = random.Random(20260101)
    base = datetime.now(timezone.utc)
    memory_types = ("fact", "decision", "preference", "procedure", "incident", "note")

    def row(n: int) -> dict[str, Any]:
        topic, other = rng.choice(_TOPICS), rng.choice(_TOPICS)
        roll = rng.random()
        if roll < 0.08:
            a, b = rng.sample(_ZH_NOUNS, 2)
            content = rng.choice(_ZH_TEMPLATES).format(a=a, b=b)
        elif roll < 0.2:
            content = (
                f"TKT-{n:06d}: the {topic} change touches "
                f"src/{other}/{rng.choice(_TOPICS)}_{n % 97}.py"
            )
        else:
            content = (
                f"The {topic} job {rng.choice(_VERBS)} {rng.choice(_ACTIONS)} "
                f"when the {other} is busy (note {n})."
            )
        project = rng.choice(_PROJECTS) if rng.random() < 0.7 else None
        repo = f"{project}-{rng.randrange(5)}" if project and rng.random() < 0.5 else None
        values = _normalize_memory_values(
            title=f"{topic.capitalize()} note {n}" if rng.random() < 0.5 else "",
            content=content,
            memory_type=rng.choice(memory_types),
            workspace=rng.choice(("work", "personal", "lab")) if rng.random() < 0.5 else None,
            project=project,
            repo=repo,
            branch="main" if repo and rng.random() < 0.1 else None,
            source=None,
        )
        stamp = (base + timedelta(microseconds=n)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        return {
            **values, "id": f"mem_{uuid.UUID(int=rng.getrandbits(128)).hex}",
            "content_hash": _content_hash(values), "revision": base_revision + n + 1,
            "created_at": stamp, "updated_at": stamp,
        }

    insert = """
        INSERT OR IGNORE INTO memories (
            id, title, content, memory_type, workspace, project, repo,
            branch, source, content_hash, revision, created_at, updated_at
        ) VALUES (
            :id, :title, :content, :memory_type, :workspace, :project, :repo,
            :branch, :source, :content_hash, :revision, :created_at, :updated_at
        )"""
    with bus._transaction() as conn:
        base_revision = conn.execute("SELECT revision FROM metadata WHERE singleton = 1").fetchone()[0]
        for start in range(0, count, 5_000):
            conn.executemany(
                insert, [row(n) for n in range(start, min(count, start + 5_000))]
            )
        conn.execute("UPDATE metadata SET revision = ? WHERE singleton = 1",
                     (base_revision + count,))


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
    parser.add_argument(
        "--scale", type=int, default=0, metavar="N",
        help="also seed N synthetic memories (latency/noise run)",
    )
    parser.add_argument("--min-recall-5", type=float)
    parser.add_argument("--min-recall-10", type=float)
    parser.add_argument("--max-context-preference-error-rate", type=float)
    parser.add_argument("--max-superseded-leak-rate", type=float)
    parser.add_argument("--max-p95-ms", type=float)
    args = parser.parse_args(argv)

    report = evaluate_dataset(args.dataset, scale=args.scale)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))

    failures: list[str] = [f"check failed: {name}" for name in report["failed_checks"]]
    gates = (
        ("recall@5", report["recall_at_5"], args.min_recall_5, "min"),
        ("recall@10", report["recall_at_10"], args.min_recall_10, "min"),
        ("context-preference error rate",
         report["context_preference_error_rate"],
         args.max_context_preference_error_rate, "max"),
        ("superseded-leak rate", report["superseded_leak_rate"],
         args.max_superseded_leak_rate, "max"),
        ("latency p95 (ms)", report["latency_p95_ms"], args.max_p95_ms, "max"),
    )
    for label, value, limit, kind in gates:
        if limit is None:
            continue
        if (kind == "min" and value < limit) or (kind == "max" and value > limit):
            op = "<" if kind == "min" else ">"
            failures.append(f"{label} {value:.4f} {op} {limit:.4f}")

    for failure in failures:
        print(f"evaluation gate failed: {failure}", file=sys.stderr)
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
