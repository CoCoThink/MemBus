"""Minimal MemBus core: SQLite memory plus lexical retrieval."""

from __future__ import annotations

import contextlib
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


_BUSY_TIMEOUT_MS = 5_000
_BEGIN_RETRY_ATTEMPTS = 5
_MEMORY_TYPES = frozenset({
    "fact",
    "decision",
    "preference",
    "procedure",
    "incident",
    "failure",
    "note",
})
_TOKEN_RE = re.compile(r"[^\W_]+(?:[_-][^\W_]+)*", re.UNICODE)
_CONTEXT_FIELDS = ("workspace", "project", "repo", "branch")
_MEMORY_COLUMNS = ("id", "title", "content", "memory_type", *_CONTEXT_FIELDS)
_MUTABLE_FIELDS = set(_MEMORY_COLUMNS) - {"id"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    workspace TEXT,
    project TEXT,
    repo TEXT,
    branch TEXT
);

CREATE TABLE IF NOT EXISTS aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED,
    title,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS memories_ai
AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts(memory_id, title, content)
    VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_au
AFTER UPDATE OF title, content ON memories BEGIN
    DELETE FROM memory_fts WHERE memory_id = old.id;
    INSERT INTO memory_fts(memory_id, title, content)
    VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad
AFTER DELETE ON memories BEGIN
    DELETE FROM memory_fts WHERE memory_id = old.id;
END;
"""


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

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._initialize()

    def put(
        self,
        *,
        content: str,
        memory_type: str,
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

        current = self.get(memory_id)
        if current is None:
            raise KeyError(memory_id)
        if not changes:
            return current

        proposed = replace(current, **changes)
        memory = Memory(
            id=current.id,
            **_normalize_memory_values(
                title=proposed.title,
                content=proposed.content,
                memory_type=proposed.memory_type,
                workspace=proposed.workspace,
                project=proposed.project,
                repo=proposed.repo,
                branch=proposed.branch,
            ),
        )

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
            return conn.execute(
                "DELETE FROM memories WHERE id = ?",
                (memory_id,),
            ).rowcount > 0

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
        types: Iterable[str] = (),
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
        normalized_types = tuple(dict.fromkeys(_memory_type(value) for value in types))

        exact = self._exact_candidates(
            query,
            context=context,
            types=normalized_types,
            limit=limit,
        )

        tokens = _extract_tokens(query)
        fts_query = _build_fts_or_query(
            _expand_tokens(tokens, self._alias_map(tokens))
        )
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
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _initialize(self) -> None:
        with contextlib.closing(self._connect()) as conn:
            self._ensure_wal(conn)
            try:
                conn.executescript(_SCHEMA)
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

    def _retry_sqlite(self, action: Callable[[], Any]) -> None:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(_BEGIN_RETRY_ATTEMPTS):
            try:
                action()
                return
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                last_error = exc
                if attempt + 1 >= _BEGIN_RETRY_ATTEMPTS:
                    break
                time.sleep(0.02 * (2**attempt))

        assert last_error is not None
        raise last_error


def _normalize_memory_values(
    *,
    title: str,
    content: str,
    memory_type: str,
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
        "memory_type": _memory_type(memory_type),
        "workspace": _clean_optional(workspace),
        "project": _clean_optional(project),
        "repo": _clean_optional(repo),
        "branch": _clean_optional(branch),
    }
    _validate_context(values)
    return values


def _memory_type(value: str) -> str:
    value = str(value)
    if value not in _MEMORY_TYPES:
        raise ValueError(f"Unsupported memory type: {value!r}")
    return value


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
