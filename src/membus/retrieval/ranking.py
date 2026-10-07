"""Transparent V1 ranking for MemBus."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from ..models import Memory


DEFAULT_WEIGHTS = {
    "lexical": 0.60,
    "scope": 0.15,
    "recency": 0.10,
    "importance": 0.10,
    "trust": 0.05,
}


def lexical_score_from_rank(rank: int, *, exact_match: bool = False) -> float:
    """Convert an FTS rank position into a stable [0, 1] relevance signal."""

    if exact_match:
        return 1.0
    if rank < 0:
        raise ValueError("rank must be non-negative")
    return 1.0 / (1.0 + 0.15 * rank)


def recency_score(
    updated_at: str,
    *,
    now: datetime | None = None,
    half_life_days: float = 30.0,
    floor: float = 0.20,
) -> float:
    now = now or datetime.now(timezone.utc)
    updated = datetime.fromisoformat(updated_at)
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)

    age_seconds = max(0.0, (now - updated).total_seconds())
    age_days = age_seconds / 86_400.0
    score = 0.5 ** (age_days / half_life_days)
    return max(floor, min(1.0, score))


def final_score(
    *,
    memory: Memory,
    lexical: float,
    scope: float,
    exact_match: bool,
    now: datetime | None = None,
) -> tuple[float, dict[str, Any]]:
    recency = recency_score(memory.updated_at, now=now)
    signals: dict[str, Any] = {
        "lexical": _bounded(lexical),
        "scope": _bounded(scope),
        "recency": _bounded(recency),
        "importance": _bounded(memory.importance),
        "trust": _bounded(memory.trust),
        "exact_match": bool(exact_match),
    }

    score = sum(
        DEFAULT_WEIGHTS[name] * float(signals[name])
        for name in DEFAULT_WEIGHTS
    )

    # Defensive against floating point noise.
    score = max(0.0, min(1.0, score))
    return score, signals


def _bounded(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))
