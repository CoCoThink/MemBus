"""Transparent lexical + scope ranking for MemBus."""

from __future__ import annotations

import math
from typing import Any


LEXICAL_WEIGHT = 0.80
SCOPE_WEIGHT = 0.20


def lexical_score_from_rank(rank: int) -> float:
    """Convert an FTS rank position into a stable [0, 1] relevance signal."""

    if rank < 0:
        raise ValueError("rank must be non-negative")
    return 1.0 / (1.0 + 0.15 * rank)


def final_score(
    *,
    lexical: float,
    scope: float,
    exact_match: bool,
) -> tuple[float, dict[str, Any]]:
    """Combine only signals with current, explicit semantics.

    V1 intentionally excludes recency, importance, confidence, and trust.
    Those signals are easy to invent but difficult to calibrate, and can demote
    old-but-valid canonical knowledge without evidence that doing so helps.
    """

    signals: dict[str, Any] = {
        "lexical": _bounded(lexical),
        "scope": _bounded(scope),
        "exact_match": bool(exact_match),
    }

    score = (
        LEXICAL_WEIGHT * float(signals["lexical"])
        + SCOPE_WEIGHT * float(signals["scope"])
    )
    return max(0.0, min(1.0, score)), signals


def _bounded(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))
