"""Domain models for MemBus.

The core models intentionally contain only metadata with current runtime
semantics. Future lifecycle or scoring fields are added only when a measured
need justifies them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable


class MemoryType(StrEnum):
    FACT = "fact"
    DECISION = "decision"
    PREFERENCE = "preference"
    PROCEDURE = "procedure"
    INCIDENT = "incident"
    FAILURE = "failure"
    NOTE = "note"


class Scope(StrEnum):
    GLOBAL = "global"
    WORKSPACE = "workspace"
    PROJECT = "project"
    REPO = "repo"
    BRANCH = "branch"


@dataclass(frozen=True, slots=True)
class SearchContext:
    workspace: str | None = None
    project: str | None = None
    repo: str | None = None
    branch: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "workspace": self.workspace,
            "project": self.project,
            "repo": self.repo,
            "branch": self.branch,
        }


@dataclass(frozen=True, slots=True)
class MemoryCreate:
    content: str
    memory_type: MemoryType | str
    scope: Scope | str
    title: str = ""
    workspace: str | None = None
    project: str | None = None
    repo: str | None = None
    branch: str | None = None
    source_type: str | None = None
    source_ref: str | None = None


@dataclass(frozen=True, slots=True)
class Memory:
    id: str
    title: str
    content: str
    memory_type: str
    scope: str
    workspace: str | None
    project: str | None
    repo: str | None
    branch: str | None
    source_type: str | None
    source_ref: str | None
    created_at: str
    updated_at: str
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    context: SearchContext = field(default_factory=SearchContext)
    types: tuple[str, ...] = ()
    limit: int = 10
    explain: bool = False
    include_deleted: bool = False

    @classmethod
    def with_types(
        cls,
        *,
        query: str,
        context: SearchContext | None = None,
        types: Iterable[MemoryType | str] = (),
        limit: int = 10,
        explain: bool = False,
        include_deleted: bool = False,
    ) -> "SearchRequest":
        return cls(
            query=query,
            context=context or SearchContext(),
            types=tuple(str(t) for t in types),
            limit=limit,
            explain=explain,
            include_deleted=include_deleted,
        )


@dataclass(frozen=True, slots=True)
class SearchResult:
    memory: Memory
    score: float
    signals: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchManyRequest:
    queries: tuple[str, ...]
    context: SearchContext = field(default_factory=SearchContext)
    types: tuple[str, ...] = ()
    limit: int = 10
    explain: bool = False
    include_deleted: bool = False

    @classmethod
    def from_queries(
        cls,
        queries: Iterable[str],
        *,
        context: SearchContext | None = None,
        types: Iterable[MemoryType | str] = (),
        limit: int = 10,
        explain: bool = False,
        include_deleted: bool = False,
    ) -> "SearchManyRequest":
        return cls(
            queries=tuple(queries),
            context=context or SearchContext(),
            types=tuple(str(t) for t in types),
            limit=limit,
            explain=explain,
            include_deleted=include_deleted,
        )
