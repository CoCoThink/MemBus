from __future__ import annotations

from pathlib import Path

from membus.evaluation import evaluate_dataset


ROOT = Path(__file__).resolve().parents[1]


def test_golden_v1_retrieval_baseline() -> None:
    report = evaluate_dataset(ROOT / "evals" / "golden_v1.json")

    assert report.query_count >= 15
    assert report.recall_at_5 >= 0.95
    assert report.recall_at_10 >= 0.95
    assert report.mrr >= 0.90
    assert report.zero_result_rate == 0.0
    assert report.wrong_scope_hit_rate == 0.0
    assert report.failed_queries == ()
