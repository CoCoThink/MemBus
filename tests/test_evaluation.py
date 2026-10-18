from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_golden_retrieval_baseline() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "evaluate.py"),
            str(ROOT / "evals" / "golden_v1.json"),
            "--min-recall-5", "0.95",
            "--min-recall-10", "0.95",
            "--max-context-preference-error-rate", "0.0",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)

    assert report["query_count"] >= 15
    assert report["recall_at_5"] >= 0.95
    assert report["recall_at_10"] >= 0.95
    assert report["mrr"] >= 0.90
    assert report["zero_result_rate"] == 0.0
    assert report["context_preference_error_rate"] == 0.0
    assert report["failed_queries"] == []
