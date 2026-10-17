"""Domain models for MemBus."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class MemoryType(StrEnum):
    FACT = "fact"
    DECISION = "decision"
    PREFERENCE = "preference"
    PROCEDURE = "procedure"
    INCIDENT = "incident"
    FAILURE = "failure"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class SearchContext:
    workspace: str | None = None
    project: str | None = None
    repo: str | None = None
    branch: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryCreate:
    content: str
    memory_type: MemoryType | str
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
    workspace: str | None
    project: str | None
    repo: str | None
    branch: str | None
    source_type: str | None
    source_ref: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    context: SearchContext = field(default_factory=SearchContext)
    types: tuple[str, ...] = ()
    limit: int = 10


@dataclass(frozen=True, slots=True)
class SearchResult:
    memory: Memory
    match: str
