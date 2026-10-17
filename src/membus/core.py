"""Minimal MemBus core: canonical SQLite memory plus lexical retrieval."""

from __future__ import annotations

import contextlib
import re
import sqlite3
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any


_TOKEN_RE = re.compile(r"[^\W_]+(?:[_-][^\W_]+)*", re.UNICODE)
_CONTEXT_FIELDS = ("workspace", "project", "repo", "branch")
_MEMORY_COLUMNS = (
    "id",
    "title",
    "content",
    "memory_type",
    *_CONTEXT_FIELDS,
)
_MUTABLE_FIELDS = set(_MEMORY_COLUMNS) - {"id"}


class MemoryType(StrEnum):
    FACT = "fact"
    DECISION = "decision"
    PREFERENCE = "preference"
    PROCEDURE = "procedure"
    INCIDENT = "incident"
    FAILURE = "failure"
    NOTE = "note"


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


class MemBus:
    """User-owned SQLite memory store."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        begin_retry_attempts: int = 5,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.begin_retry_attempts = begin_retry_attempts
        self._initialize()

    def put(
        self,
        *,
        content: str,
        memory_type: MemoryType | str,
        title: str = "",
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
    ) -> Memory:
        values = _normalize_memory_values(
            title=title,
            content=content,
            memory_type=memory_type,
            workspace=workspace,
            project=project,
            repo=repo,
            branch=branch,
        )
        memory = Memory(id=f"mem_{uuid.uuid4().hex}", **values)

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO memories (
                    id, title, content, memory_type,
                    workspace, project, repo, branch
                ) VALUES (
                    :id, :title, :content, :memory_type,
                    :workspace, :project, :repo, :branch
                )
                """,
                _memory_params(memory),
            )
        return memory

    def get(self, memory_id: str) -> Memory | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
        return _memory_from_row(row) if row is not None else None

    def update(self, memory_id: str, **changes: Any) -> Memory:
        unknown = set(changes) - _MUTABLE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported mutable fields: {sorted(unknown)}")
        if not changes:
            current = self.get(memory_id)
            if current is None:
                raise KeyError(memory_id)
            return current

        current = self.get(memory_id)
        if current is None:
            raise KeyError(memory_id)

        proposed = replace(current, **changes)
        values = _normalize_memory_values(
            title=proposed.title,
            content=proposed.content,
            memory_type=proposed.memory_type,
            workspace=proposed.workspace,
            project=proposed.project,
            repo=proposed.repo,
            branch=proposed.branch,
        )
        memory = Memory(id=current.id, **values)

        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE memories
                SET
                    title = :title,
                    content = :content,
                    memory_type = :memory_type,
                    workspace = :workspace,
                    project = :project,
                    repo = :repo,
                    branch = :branch
                WHERE id = :id
                """,
                _memory_params(memory),
            )
        return memory

    def delete(self, memory_id: str) -> bool:
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE id = ?",
                (memory_id,),
            )
            return cursor.rowcount > 0

    def put_alias(self, alias: str, canonical: str) -> None:
        alias = _normalize_text(alias)
        canonical = _normalize_text(canonical)
        if not alias or not canonical:
            raise ValueError("alias and canonical must be non-empty")

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO aliases(alias, canonical)
                VALUES (?, ?)
                ON CONFLICT(alias) DO UPDATE SET canonical = excluded.canonical
                """,
                (alias, canonical),
            )

    def search(
        self,
        query: str,
        *,
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        types: Iterable[MemoryType | str] = (),
        limit: int = 10,
    ) -> list[Memory]:
        query = _normalize_text(query)
        if not query:
            return []
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        context = {
            "workspace": _clean_optional(workspace),
            "project": _clean_optional(project),
            "repo": _clean_optional(repo),
            "branch": _clean_optional(branch),
        }
        _validate_context(context)

        normalized_types = tuple(
            dict.fromkeys(str(MemoryType(str(value))) for value in types)
        )

        exact = self._exact_candidates(
            query,
            context=context,
            types=normalized_types,
            limit=limit,
        )

        tokens = _extract_tokens(query)
        aliases = self._alias_map(tokens)
        fts_query = _build_fts_or_query(_expand_tokens(tokens, aliases))
        fts = (
            self._fts_candidates(
                fts_query,
                context=context,
                types=normalized_types,
                limit=limit + len(exact),
            )
            if fts_query is not None
            else []
        )

        results = list(exact)
        seen = {memory.id for memory in exact}
        for memory in fts:
            if memory.id not in seen:
                results.append(memory)
                seen.add(memory.id)
            if len(results) >= limit:
                break
        return results[:limit]

    def backup(self, destination: str | Path) -> Path:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        with contextlib.closing(self._connect()) as source:
            with contextlib.closing(sqlite3.connect(destination_path)) as target:
                source.backup(target)
        return destination_path

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            self._begin_with_retry(conn)
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _initialize(self) -> None:
        with contextlib.closing(self._connect()) as conn:
            self._ensure_wal(conn)
            schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            try:
                conn.executescript(schema)
            except sqlite3.OperationalError as exc:
                if "fts5" in str(exc).lower():
                    raise RuntimeError(
                        "This SQLite build does not provide FTS5, which MemBus requires."
                    ) from exc
                raise

    def _exact_candidates(
        self,
        query: str,
        *,
        context: dict[str, str | None],
        types: tuple[str, ...],
        limit: int,
    ) -> list[Memory]:
        where = [
            """
            (
                instr(lower(m.title), lower(?)) > 0
                OR instr(lower(m.content), lower(?)) > 0
            )
            """
        ]
        params: list[Any] = [query, query]
        _append_filters(where, params, context=context, types=types)

        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT m.*
                FROM memories AS m
                WHERE {' AND '.join(where)}
                ORDER BY m.id ASC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def _fts_candidates(
        self,
        match_query: str,
        *,
        context: dict[str, str | None],
        types: tuple[str, ...],
        limit: int,
    ) -> list[Memory]:
        where = ["memory_fts MATCH ?"]
        params: list[Any] = [match_query]
        _append_filters(where, params, context=context, types=types)

        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT m.*
                FROM memory_fts
                JOIN memories AS m ON m.id = memory_fts.memory_id
                WHERE {' AND '.join(where)}
                ORDER BY bm25(memory_fts) ASC, m.id ASC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def _alias_map(self, tokens: Iterable[str]) -> dict[str, str]:
        normalized = tuple(dict.fromkeys(token.casefold() for token in tokens if token))
        if not normalized:
            return {}

        placeholders = ",".join("?" for _ in normalized)
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT alias, canonical
                FROM aliases
                WHERE alias IN ({placeholders})
                """,
                normalized,
            ).fetchall()

        return {
            str(row["alias"]).casefold(): str(row["canonical"]).casefold()
            for row in rows
        }

    def _ensure_wal(self, conn: sqlite3.Connection) -> None:
        self._retry_sqlite(
            lambda: _require_wal(conn.execute("PRAGMA journal_mode = WAL").fetchone())
        )

    def _begin_with_retry(self, conn: sqlite3.Connection) -> None:
        self._retry_sqlite(lambda: conn.execute("BEGIN IMMEDIATE"))

    def _retry_sqlite(self, action: Any) -> None:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(self.begin_retry_attempts):
            try:
                action()
                return
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                last_error = exc
                if attempt + 1 >= self.begin_retry_attempts:
                    break
                time.sleep(0.02 * (2**attempt))

        assert last_error is not None
        raise last_error


def _normalize_memory_values(
    *,
    title: str,
    content: str,
    memory_type: MemoryType | str,
    workspace: str | None,
    project: str | None,
    repo: str | None,
    branch: str | None,
) -> dict[str, Any]:
    content = content.strip()
    if not content:
        raise ValueError("content must contain non-whitespace text")

    values: dict[str, Any] = {
        "title": title.strip(),
        "content": content,
        "memory_type": str(MemoryType(str(memory_type))),
        "workspace": _clean_optional(workspace),
        "project": _clean_optional(project),
        "repo": _clean_optional(repo),
        "branch": _clean_optional(branch),
    }
    _validate_context(values)
    return values


def _validate_context(values: dict[str, Any]) -> None:
    if values.get("branch") and not values.get("repo"):
        raise ValueError("branch requires repo")


def _append_filters(
    where: list[str],
    params: list[Any],
    *,
    context: dict[str, str | None],
    types: tuple[str, ...],
) -> None:
    for column in _CONTEXT_FIELDS:
        value = context[column]
        if value is not None:
            where.append(f"(m.{column} IS NULL OR m.{column} = ?)")
            params.append(value)

    if types:
        placeholders = ",".join("?" for _ in types)
        where.append(f"m.memory_type IN ({placeholders})")
        params.extend(types)


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _extract_tokens(query: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in _TOKEN_RE.finditer(query)))


def _expand_tokens(tokens: Iterable[str], aliases: dict[str, str]) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        for candidate in (token, aliases.get(token)):
            if candidate is not None and candidate not in seen:
                seen.add(candidate)
                expanded.append(candidate)
    return expanded


def _build_fts_or_query(tokens: Iterable[str]) -> str | None:
    parts = [
        f'"{token.replace(chr(34), chr(34) * 2)}"'
        for token in tokens
        if token
    ]
    return " OR ".join(parts) if parts else None


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _memory_params(memory: Memory) -> dict[str, Any]:
    return {column: getattr(memory, column) for column in _MEMORY_COLUMNS}


def _memory_from_row(row: sqlite3.Row) -> Memory:
    return Memory(**{column: row[column] for column in _MEMORY_COLUMNS})


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _require_wal(row: sqlite3.Row | tuple[Any, ...] | None) -> None:
    if row is None or str(row[0]).casefold() != "wal":
        raise RuntimeError("SQLite refused WAL journal mode")
