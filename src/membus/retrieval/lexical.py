"""Deterministic lexical query utilities."""

from __future__ import annotations

import re
from collections.abc import Iterable


_TOKEN_RE = re.compile(r"[^\W_]+(?:[_-][^\W_]+)*", re.UNICODE)


def normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def extract_tokens(query: str) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []

    for match in _TOKEN_RE.finditer(normalize_text(query)):
        token = match.group(0)
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)

    return tokens


def expand_tokens(tokens: Iterable[str], aliases: dict[str, str]) -> list[str]:
    seen: set[str] = set()
    expanded: list[str] = []

    for token in tokens:
        for candidate in (token, aliases.get(token)):
            if candidate is None:
                continue
            normalized = normalize_text(candidate)
            if normalized and normalized not in seen:
                seen.add(normalized)
                expanded.append(normalized)

    return expanded


def build_fts_or_query(tokens: Iterable[str]) -> str | None:
    parts = [
        f'"{token.replace(chr(34), chr(34) * 2)}"'
        for token in tokens
        if token
    ]
    return " OR ".join(parts) if parts else None
