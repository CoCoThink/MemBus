"""Application service for MemBus V1."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from ..db import Database
from ..models import (
    Memory,
    MemoryCreate,
    MemoryType,
    Scope,
    SearchManyRequest,
    SearchRequest,
    SearchResult,
)
from ..repository import MemoryRepository
from ..retrieval.lexical import (
    build_fts_or_query,
    expand_tokens,
    extract_tokens,
    normalize_text,
)
from ..retrieval.ranking import final_score, lexical_score_from_rank
from ..retrieval.scope import scope_affinity, validate_scope


_MUTABLE_FIELDS = {
    "title",
    "content",
    "memory_type",
    "scope",
    "workspace",
    "project",
    "repo",
    "branch",
    "importance",
    "confidence",
    "trust",
    "source_type",
    "source_ref",
    "valid_from",
    "valid_until",
    "supersedes_id",
}


class MemoryService:
    """Stable domain API used by protocol adapters."""

    def __init__(
        self,
        db: Database,
        *,
        retrieval_log_mode: str = "off",
    ) -> None:
        if retrieval_log_mode not in {"off", "metadata-only", "full"}:
            raise ValueError(
                "retrieval_log_mode must be one of: off, metadata-only, full"
            )

        self.db = db
        self.db.initialize()
        self.repository = MemoryRepository(db)
        self.retrieval_log_mode = retrieval_log_mode

    def put(self, create: MemoryCreate, *, actor: str | None = None) -> Memory:
        normalized = self._validate_create(create)
        now = _utcnow()

        memory = Memory(
            id=f"mem_{uuid.uuid4().hex}",
            title=normalized.title.strip(),
            content=normalized.content.strip(),
            memory_type=str(MemoryType(str(normalized.memory_type))),
            scope=str(Scope(str(normalized.scope))),
            workspace=_clean_optional(normalized.workspace),
            project=_clean_optional(normalized.project),
            repo=_clean_optional(normalized.repo),
            branch=_clean_optional(normalized.branch),
            status="active",
            importance=float(normalized.importance),
            confidence=float(normalized.confidence),
            trust=float(normalized.trust),
            source_type=_clean_optional(normalized.source_type),
            source_ref=_clean_optional(normalized.source_ref),
            valid_from=_clean_optional(normalized.valid_from),
            valid_until=_clean_optional(normalized.valid_until),
            supersedes_id=_clean_optional(normalized.supersedes_id),
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        self.repository.create(memory, actor=actor)
        return memory

    def get(self, memory_id: str, *, include_deleted: bool = False) -> Memory | None:
        return self.repository.get(memory_id, include_deleted=include_deleted)

    def update(
        self,
        memory_id: str,
        *,
        actor: str | None = None,
        **changes: Any,
    ) -> Memory:
        unknown = set(changes) - _MUTABLE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported mutable fields: {sorted(unknown)}")

        current = self.repository.get(memory_id, include_deleted=True)
        if current is None:
            raise KeyError(memory_id)
        if current.status == "deleted":
            raise ValueError("Cannot update a logically deleted memory")
        if not changes:
            return current

        proposed = replace(
            current,
            **changes,
            updated_at=_utcnow(),
        )

        create_shape = MemoryCreate(
            title=proposed.title,
            content=proposed.content,
            memory_type=proposed.memory_type,
            scope=proposed.scope,
            workspace=proposed.workspace,
            project=proposed.project,
            repo=proposed.repo,
            branch=proposed.branch,
            importance=proposed.importance,
            confidence=proposed.confidence,
            trust=proposed.trust,
            source_type=proposed.source_type,
            source_ref=proposed.source_ref,
            valid_from=proposed.valid_from,
            valid_until=proposed.valid_until,
            supersedes_id=proposed.supersedes_id,
        )
        normalized = self._validate_create(create_shape)

        final_memory = replace(
            proposed,
            title=normalized.title.strip(),
            content=normalized.content.strip(),
            memory_type=str(MemoryType(str(normalized.memory_type))),
            scope=str(Scope(str(normalized.scope))),
            workspace=_clean_optional(normalized.workspace),
            project=_clean_optional(normalized.project),
            repo=_clean_optional(normalized.repo),
            branch=_clean_optional(normalized.branch),
            source_type=_clean_optional(normalized.source_type),
            source_ref=_clean_optional(normalized.source_ref),
            valid_from=_clean_optional(normalized.valid_from),
            valid_until=_clean_optional(normalized.valid_until),
            supersedes_id=_clean_optional(normalized.supersedes_id),
        )

        changed_payload = {
            key: getattr(final_memory, key)
            for key in changes
            if getattr(current, key) != getattr(final_memory, key)
        }
        if not changed_payload:
            return current

        return self.repository.update(
            final_memory,
            changed_fields=changed_payload,
            actor=actor,
        )

    def delete(self, memory_id: str, *, actor: str | None = None) -> bool:
        return self.repository.logical_delete(
            memory_id,
            deleted_at=_utcnow(),
            actor=actor,
        )

    def put_alias(self, alias: str, canonical: str) -> None:
        alias = normalize_text(alias)
        canonical = normalize_text(canonical)
        if not alias or not canonical:
            raise ValueError("alias and canonical must be non-empty")
        self.repository.put_alias(alias, canonical, created_at=_utcnow())

    def search(self, request: SearchRequest) -> list[SearchResult]:
        started = time.perf_counter()
        results, candidate_count = self._search_unlogged(request)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._log_search(
            query=request.query,
            request=request,
            candidate_count=candidate_count,
            results=results,
            latency_ms=elapsed_ms,
        )
        return results

    def search_many(self, request: SearchManyRequest) -> list[SearchResult]:
        if not request.queries:
            return []
        if not (1 <= request.limit <= 100):
            raise ValueError("limit must be between 1 and 100")

        started = time.perf_counter()
        merged: dict[str, tuple[SearchResult, int]] = {}
        total_candidates = 0

        for query in request.queries:
            single = SearchRequest(
                query=query,
                context=request.context,
                types=request.types,
                limit=max(request.limit * 5, 25),
                explain=True,
                include_deleted=request.include_deleted,
            )
            results, candidate_count = self._search_unlogged(single)
            total_candidates += candidate_count

            for result in results:
                previous = merged.get(result.memory.id)
                if previous is None:
                    merged[result.memory.id] = (result, 1)
                    continue

                best, hits = previous
                if result.score > best.score:
                    best = result
                merged[result.memory.id] = (best, hits + 1)

        fused: list[SearchResult] = []
        for best, hits in merged.values():
            bonus = min(0.10, 0.03 * max(0, hits - 1))
            score = min(1.0, best.score + bonus)
            signals = dict(best.signals)
            signals["query_hits"] = hits
            signals["multi_query_bonus"] = bonus
            fused.append(
                SearchResult(
                    memory=best.memory,
                    score=score,
                    signals=signals if request.explain else {},
                )
            )

        fused.sort(
            key=lambda result: (result.score, result.memory.updated_at),
            reverse=True,
        )
        final_results = fused[: request.limit]
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        self._log_many_search(
            queries=request.queries,
            request=request,
            candidate_count=total_candidates,
            results=final_results,
            latency_ms=elapsed_ms,
        )
        return final_results

    def _search_unlogged(
        self,
        request: SearchRequest,
    ) -> tuple[list[SearchResult], int]:
        query = normalize_text(request.query)
        if not query:
            return [], 0
        if not (1 <= request.limit <= 100):
            raise ValueError("limit must be between 1 and 100")

        types = self._normalize_types(request.types)
        pool_limit = min(500, max(50, request.limit * 8))

        exact_memories = self.repository.exact_candidates(
            query,
            types=types,
            include_deleted=request.include_deleted,
            limit=pool_limit,
        )

        tokens = extract_tokens(query)
        aliases = self.repository.alias_map(tokens)
        expanded_tokens = expand_tokens(tokens, aliases)
        fts_query = build_fts_or_query(expanded_tokens)

        fts_memories: list[tuple[Memory, float]] = []
        if fts_query is not None:
            fts_memories = self.repository.fts_candidates(
                fts_query,
                types=types,
                include_deleted=request.include_deleted,
                limit=pool_limit,
            )

        candidates: dict[str, dict[str, Any]] = {}

        for memory in exact_memories:
            candidates[memory.id] = {
                "memory": memory,
                "exact_match": True,
                "fts_rank": None,
            }

        for rank, (memory, raw_bm25) in enumerate(fts_memories):
            item = candidates.setdefault(
                memory.id,
                {
                    "memory": memory,
                    "exact_match": False,
                    "fts_rank": rank,
                },
            )
            previous_rank = item["fts_rank"]
            if previous_rank is None or rank < previous_rank:
                item["fts_rank"] = rank
            item["raw_bm25"] = raw_bm25

        ranked: list[SearchResult] = []
        for item in candidates.values():
            memory: Memory = item["memory"]
            scope = scope_affinity(memory, request.context)
            if not scope.eligible:
                continue

            exact_match = bool(item["exact_match"])
            if exact_match:
                lexical = 1.0
            else:
                fts_rank = item["fts_rank"]
                if fts_rank is None:
                    continue
                lexical = lexical_score_from_rank(int(fts_rank))

            score, signals = final_score(
                memory=memory,
                lexical=lexical,
                scope=scope.score,
                exact_match=exact_match,
            )
            signals["scope_reason"] = scope.reason
            if "raw_bm25" in item:
                signals["raw_bm25"] = item["raw_bm25"]
            if aliases:
                signals["alias_expansion"] = {
                    token: sorted(values)
                    for token, values in aliases.items()
                    if values
                }

            ranked.append(
                SearchResult(
                    memory=memory,
                    score=score,
                    signals=signals if request.explain else {},
                )
            )

        ranked.sort(
            key=lambda result: (result.score, result.memory.updated_at),
            reverse=True,
        )
        return ranked[: request.limit], len(candidates)

    def _log_search(
        self,
        *,
        query: str,
        request: SearchRequest,
        candidate_count: int,
        results: list[SearchResult],
        latency_ms: float,
    ) -> None:
        if self.retrieval_log_mode == "off":
            return

        self.repository.append_retrieval_log(
            query=query if self.retrieval_log_mode == "full" else None,
            scope=request.context.as_dict(),
            candidate_count=candidate_count,
            result_ids=[result.memory.id for result in results],
            latency_ms=latency_ms,
            debug={
                "types": list(request.types),
                "limit": request.limit,
            },
            created_at=_utcnow(),
        )

    def _log_many_search(
        self,
        *,
        queries: tuple[str, ...],
        request: SearchManyRequest,
        candidate_count: int,
        results: list[SearchResult],
        latency_ms: float,
    ) -> None:
        if self.retrieval_log_mode == "off":
            return

        query_value = (
            json.dumps(list(queries), ensure_ascii=False)
            if self.retrieval_log_mode == "full"
            else None
        )
        self.repository.append_retrieval_log(
            query=query_value,
            scope=request.context.as_dict(),
            candidate_count=candidate_count,
            result_ids=[result.memory.id for result in results],
            latency_ms=latency_ms,
            debug={
                "query_count": len(queries),
                "types": list(request.types),
                "limit": request.limit,
            },
            created_at=_utcnow(),
        )

    @staticmethod
    def _normalize_types(types: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in types:
            normalized.append(str(MemoryType(str(value))))
        return tuple(dict.fromkeys(normalized))

    @staticmethod
    def _validate_create(create: MemoryCreate) -> MemoryCreate:
        if not create.content or not create.content.strip():
            raise ValueError("content must contain non-whitespace text")

        MemoryType(str(create.memory_type))
        validate_scope(create)

        for name in ("importance", "confidence", "trust"):
            value = float(getattr(create, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")

        return create


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
