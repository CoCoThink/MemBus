"""Application service for MemBus."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from ..models import Memory, MemoryCreate, MemoryType, SearchRequest, SearchResult
from ..retrieval.lexical import (
    build_fts_or_query,
    expand_tokens,
    extract_tokens,
    normalize_text,
)
from ..store import SQLiteStore


_MUTABLE_FIELDS = {
    "title",
    "content",
    "memory_type",
    "workspace",
    "project",
    "repo",
    "branch",
    "source_type",
    "source_ref",
}


class MemoryService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.store.initialize()

    def put(self, create: MemoryCreate) -> Memory:
        normalized = _normalize_create(create)
        now = _utcnow()

        memory = Memory(
            id=f"mem_{uuid.uuid4().hex}",
            title=normalized.title,
            content=normalized.content,
            memory_type=str(MemoryType(str(normalized.memory_type))),
            workspace=normalized.workspace,
            project=normalized.project,
            repo=normalized.repo,
            branch=normalized.branch,
            source_type=normalized.source_type,
            source_ref=normalized.source_ref,
            created_at=now,
            updated_at=now,
        )
        self.store.create(memory)
        return memory

    def get(self, memory_id: str) -> Memory | None:
        return self.store.get(memory_id)

    def update(self, memory_id: str, **changes: Any) -> Memory:
        unknown = set(changes) - _MUTABLE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported mutable fields: {sorted(unknown)}")

        current = self.store.get(memory_id)
        if current is None:
            raise KeyError(memory_id)
        if not changes:
            return current

        proposed = replace(current, **changes, updated_at=_utcnow())
        normalized = _normalize_create(
            MemoryCreate(
                title=proposed.title,
                content=proposed.content,
                memory_type=proposed.memory_type,
                workspace=proposed.workspace,
                project=proposed.project,
                repo=proposed.repo,
                branch=proposed.branch,
                source_type=proposed.source_type,
                source_ref=proposed.source_ref,
            )
        )

        return self.store.update(
            replace(
                proposed,
                title=normalized.title,
                content=normalized.content,
                memory_type=str(MemoryType(str(normalized.memory_type))),
                workspace=normalized.workspace,
                project=normalized.project,
                repo=normalized.repo,
                branch=normalized.branch,
                source_type=normalized.source_type,
                source_ref=normalized.source_ref,
            )
        )

    def delete(self, memory_id: str) -> bool:
        return self.store.delete(memory_id)

    def put_alias(self, alias: str, canonical: str) -> None:
        alias = normalize_text(alias)
        canonical = normalize_text(canonical)
        if not alias or not canonical:
            raise ValueError("alias and canonical must be non-empty")
        self.store.put_alias(alias, canonical)

    def search(self, request: SearchRequest) -> list[SearchResult]:
        query = normalize_text(request.query)
        if not query:
            return []
        if not (1 <= request.limit <= 100):
            raise ValueError("limit must be between 1 and 100")

        _validate_context(request.context)
        types = tuple(
            dict.fromkeys(str(MemoryType(str(value))) for value in request.types)
        )

        exact = self.store.exact_candidates(
            query,
            context=request.context,
            types=types,
            limit=request.limit,
        )

        tokens = extract_tokens(query)
        aliases = self.store.alias_map(tokens)
        expanded_tokens = expand_tokens(tokens, aliases)
        fts_query = build_fts_or_query(expanded_tokens)
        fts = (
            self.store.fts_candidates(
                fts_query,
                context=request.context,
                types=types,
                limit=request.limit,
            )
            if fts_query is not None
            else []
        )

        results: list[SearchResult] = []
        seen: set[str] = set()

        for memory in exact:
            seen.add(memory.id)
            results.append(SearchResult(memory=memory, match="exact"))

        for memory in fts:
            if memory.id in seen:
                continue
            results.append(SearchResult(memory=memory, match="fts"))
            if len(results) >= request.limit:
                break

        return results[: request.limit]


def _normalize_create(create: MemoryCreate) -> MemoryCreate:
    content = create.content.strip()
    if not content:
        raise ValueError("content must contain non-whitespace text")

    memory_type = str(MemoryType(str(create.memory_type)))
    normalized = MemoryCreate(
        title=create.title.strip(),
        content=content,
        memory_type=memory_type,
        workspace=_clean_optional(create.workspace),
        project=_clean_optional(create.project),
        repo=_clean_optional(create.repo),
        branch=_clean_optional(create.branch),
        source_type=_clean_optional(create.source_type),
        source_ref=_clean_optional(create.source_ref),
    )
    _validate_context(normalized)
    return normalized


def _validate_context(value: Any) -> None:
    if getattr(value, "branch", None) and not getattr(value, "repo", None):
        raise ValueError("branch requires repo")


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
