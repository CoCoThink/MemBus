"""Deterministic lexical query utilities."""

from __future__ import annotations

import re
from collections.abc import Iterable


_TOKEN_RE = re.compile(r"[^\W_]+(?:[_-][^\W_]+)*", re.UNICODE)


def normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def extract_tokens(query: str) -> list[str]:
    """Extract safe lexical terms without accepting raw FTS syntax."""

    seen: set[str] = set()
    tokens: list[str] = []

    for match in _TOKEN_RE.finditer(normalize_text(query)):
        token = match.group(0)
        if not token or token in seen:
            continue
        seen.add(token)
        tokens.append(token)

    return tokens


def expand_tokens(tokens: Iterable[str], aliases: dict[str, set[str]]) -> list[str]:
    seen: set[str] = set()
    expanded: list[str] = []

    for token in tokens:
        for candidate in (token, *sorted(aliases.get(token, ()))):
            normalized = normalize_text(candidate)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            expanded.append(normalized)

    return expanded


def build_fts_or_query(tokens: Iterable[str]) -> str | None:
    """Build a safe FTS5 OR expression from already-tokenized terms."""

    parts: list[str] = []
    for token in tokens:
        escaped = token.replace('"', '""')
        if escaped:
            parts.append(f'"{escaped}"')

    if not parts:
        return None

    return " OR ".join(parts)
