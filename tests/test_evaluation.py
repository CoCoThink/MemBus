from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVALUATE = [sys.executable, str(ROOT / "tools" / "evaluate.py"),
            str(ROOT / "evals" / "golden_v1.json")]


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*EVALUATE, *args], cwd=ROOT, check=check, capture_output=True, text=True,
    )


def test_golden_retrieval_baseline() -> None:
    report = json.loads(run(
        "--min-recall-5", "0.95",
        "--min-recall-10", "0.95",
        "--max-context-preference-error-rate", "0.0",
        "--max-superseded-leak-rate", "0.0",
    ).stdout)

    assert report["query_count"] >= 25
    assert report["recall_at_5"] >= 0.95
    assert report["recall_at_10"] >= 0.95
    assert report["mrr"] >= 0.90
    assert report["zero_result_rate"] == 0.0
    assert report["context_preference_error_rate"] == 0.0
    assert report["superseded_leak_case_count"] >= 2
    assert report["superseded_leak_rate"] == 0.0
    assert report["list_case_count"] >= 3
    assert report["failed_checks"] == []
    assert report["failed_queries"] == []


def test_scale_run_reports_latency_without_list_checks() -> None:
    report = json.loads(run("--scale", "2000", "--max-p95-ms", "1000").stdout)
    assert report["synthetic_memories"] == 2000
    assert report["list_case_count"] == 0
    assert report["failed_checks"] == []
    assert report["latency_p95_ms"] > 0


def test_gate_failure_exits_with_status_2() -> None:
    result = run("--max-p95-ms", "0.0000001", check=False)
    assert result.returncode == 2
    assert "latency p95" in result.stderr
