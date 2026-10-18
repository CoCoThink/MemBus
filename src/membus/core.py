"""Minimal MemBus core: SQLite memory plus lexical retrieval."""

from __future__ import annotations

import contextlib
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
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
_HAN_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_CONTEXT_FIELDS = ("workspace", "project", "repo", "branch")
_USER_FIELDS = ("title", "content", "memory_type", *_CONTEXT_FIELDS, "source")
_MEMORY_COLUMNS = ("id", *_USER_FIELDS, "created_at", "updated_at", "deleted_at")
_MUTABLE_FIELDS = set(_USER_FIELDS)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    workspace TEXT, project TEXT, repo TEXT, branch TEXT,
    source TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED, title, content,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS memories_ai
AFTER INSERT ON memories WHEN new.deleted_at IS NULL BEGIN
    INSERT INTO memory_fts(memory_id, title, content)
    VALUES (new.id, new.title, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au
AFTER UPDATE OF title, content, deleted_at ON memories BEGIN
    DELETE FROM memory_fts WHERE memory_id = old.id;
    INSERT INTO memory_fts(memory_id, title, content)
    SELECT new.id, new.title, new.content WHERE new.deleted_at IS NULL;
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
    source: str | None
    created_at: str
    updated_at: str
    deleted_at: str | None


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
        source: str | None = None,
    ) -> Memory:
        values = _normalize_memory_values(
            title=title, content=content, memory_type=memory_type,
            workspace=workspace, project=project, repo=repo, branch=branch,
            source=source,
        )
        now = _utc_now()
        memory = Memory(
            id=f"mem_{uuid.uuid4().hex}", **values,
            created_at=now, updated_at=now, deleted_at=None,
        )
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO memories (
                    id, title, content, memory_type, workspace, project,
                    repo, branch, source, created_at, updated_at, deleted_at
                ) VALUES (
                    :id, :title, :content, :memory_type, :workspace, :project,
                    :repo, :branch, :source, :created_at, :updated_at, :deleted_at
                )
                """,
                _memory_params(memory),
            )
        return memory

    def get(self, memory_id: str) -> Memory | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ? AND deleted_at IS NULL",
                (memory_id,),
            ).fetchone()
        return _memory_from_row(row) if row is not None else None

    def update(self, memory_id: str, **changes: Any) -> Memory:
        unknown = set(changes) - _MUTABLE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported mutable fields: {sorted(unknown)}")

        # Keep the read and write under the same BEGIN IMMEDIATE lock.
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ? AND deleted_at IS NULL",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise KeyError(memory_id)
            current = _memory_from_row(row)
            if not changes:
                return current

            proposed = replace(current, **changes)
            normalized = _normalize_memory_values(
                title=proposed.title,
                content=proposed.content,
                memory_type=proposed.memory_type,
                workspace=proposed.workspace,
                project=proposed.project,
                repo=proposed.repo,
                branch=proposed.branch,
                source=proposed.source,
            )
            memory = Memory(
                id=current.id, **normalized,
                created_at=current.created_at,
                updated_at=_utc_now(),
                deleted_at=None,
            )
            cursor = conn.execute(
                """
                UPDATE memories SET
                    title = :title, content = :content,
                    memory_type = :memory_type, workspace = :workspace,
                    project = :project, repo = :repo, branch = :branch,
                    source = :source, updated_at = :updated_at
                WHERE id = :id AND deleted_at IS NULL
                """,
                _memory_params(memory),
            )
            if cursor.rowcount != 1:
                raise KeyError(memory_id)
        return memory

    def delete(self, memory_id: str) -> bool:
        """Soft-delete a memory; repeated deletion returns False."""
        now = _utc_now()
        with self._transaction() as conn:
            return conn.execute(
                """UPDATE memories SET deleted_at = ?, updated_at = ?
                   WHERE id = ? AND deleted_at IS NULL""",
                (now, now, memory_id),
            ).rowcount > 0

    def restore(self, memory_id: str) -> bool:
        """Restore a soft-deleted memory; active/missing IDs return False."""
        with self._transaction() as conn:
            return conn.execute(
                """UPDATE memories SET deleted_at = NULL, updated_at = ?
                   WHERE id = ? AND deleted_at IS NOT NULL""",
                (_utc_now(), memory_id),
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
        strict: bool = False,
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
            strict=strict,
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
                strict=strict,
            )
            if fts_query is not None
            else []
        )

        # Bounded Chinese n-gram substring fallback for reordered queries.
        # Retains the existing FTS index and needs no extra dependency.
        cjk = self._cjk_candidates(
            query, context=context, types=normalized_types,
            limit=limit + len(exact) + len(fts), strict=strict,
        )
        exact_preferred, exact_fallback = _partition_context(exact, context)
        fts_preferred, fts_fallback = _partition_context(fts, context)
        cjk_preferred, cjk_fallback = _partition_context(cjk, context)

        results: list[Memory] = []
        seen: set[str] = set()
        for group in (
            exact_preferred,
            fts_preferred,
            cjk_preferred,
            exact_fallback,
            fts_fallback,
            cjk_fallback,
        ):
            for memory in group:
                if memory.id in seen:
                    continue
                results.append(memory)
                seen.add(memory.id)
                if len(results) >= limit:
                    return results
        return results

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
        strict: bool,
    ) -> list[Memory]:
        where = ["m.deleted_at IS NULL",
            """
            (
                instr(lower(m.title), lower(?)) > 0
                OR instr(lower(m.content), lower(?)) > 0
            )
            """
        ]
        params: list[Any] = [query, query]
        _append_type_filter(where, params, types)
        if strict:
            _append_strict_context(where, params, context)
        context_order, context_params = _context_order(context)

        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT m.*
                FROM memories AS m
                WHERE {' AND '.join(where)}
                ORDER BY {context_order}, m.id ASC
                LIMIT ?
                """,
                [*params, *context_params, limit],
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def _fts_candidates(
        self,
        match_query: str,
        *,
        context: dict[str, str | None],
        types: tuple[str, ...],
        limit: int,
        strict: bool,
    ) -> list[Memory]:
        where = ["memory_fts MATCH ?", "m.deleted_at IS NULL"]
        params: list[Any] = [match_query]
        _append_type_filter(where, params, types)
        if strict:
            _append_strict_context(where, params, context)
        context_order, context_params = _context_order(context)

        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT m.*
                FROM memory_fts
                JOIN memories AS m ON m.id = memory_fts.memory_id
                WHERE {' AND '.join(where)}
                ORDER BY {context_order}, bm25(memory_fts) ASC, m.id ASC
                LIMIT ?
                """,
                [*params, *context_params, limit],
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def _cjk_candidates(
        self,
        query: str,
        *,
        context: dict[str, str | None],
        types: tuple[str, ...],
        limit: int,
        strict: bool,
    ) -> list[Memory]:
        # One 3-gram retrieves words inside reordered Chinese sentences;
        # two 2-grams retrieve separated short words, e.g. "支付部署".
        trigrams = _cjk_ngrams(query, 3)[:12]
        bigrams = _cjk_ngrams(query, 2)[:12]
        if not trigrams and len(bigrams) < 2:
            return []

        score_terms: list[str] = []
        score_params: list[str] = []
        for weight, grams in ((4, trigrams), (1, bigrams)):
            for gram in grams:
                score_terms.append(
                    "(CASE WHEN instr(m.title, ?) > 0 "
                    f"OR instr(m.content, ?) > 0 THEN {weight} ELSE 0 END)"
                )
                score_params.extend((gram, gram))
        score_sql = " + ".join(score_terms)
        where = ["m.deleted_at IS NULL"]
        params: list[Any] = []
        _append_type_filter(where, params, types)
        if strict:
            _append_strict_context(where, params, context)
        context_order, context_params = _context_order(context)

        # Bounded to 24 query grams and parameterized throughout.
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT m.*, ({score_sql}) AS cjk_score
                FROM memories AS m
                WHERE {' AND '.join(where)}
                ORDER BY cjk_score DESC, {context_order}, m.id ASC
                LIMIT ?
                """,
                [*score_params, *params, *context_params, limit],
            ).fetchall()
        return [
            _memory_from_row(row) for row in rows if row["cjk_score"] >= 2
        ]

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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalize_memory_values(
    *,
    title: str,
    content: str,
    memory_type: str,
    workspace: str | None,
    project: str | None,
    repo: str | None,
    branch: str | None,
    source: str | None,
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
        "source": _clean_optional(source),
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


def _append_type_filter(
    where: list[str],
    params: list[Any],
    types: tuple[str, ...],
) -> None:
    if types:
        placeholders = ",".join("?" for _ in types)
        where.append(f"m.memory_type IN ({placeholders})")
        params.extend(types)


def _append_strict_context(
    where: list[str],
    params: list[Any],
    context: dict[str, str | None],
) -> None:
    # An omitted context dimension must not admit narrower-scope memories.
    for column in _CONTEXT_FIELDS:
        value = context[column]
        if value is None:
            where.append(f"m.{column} IS NULL")
        else:
            where.append(f"(m.{column} IS NULL OR m.{column} = ?)")
            params.append(value)


def _context_order(
    context: dict[str, str | None],
) -> tuple[str, list[Any]]:
    conflict_terms: list[str] = []
    match_terms: list[str] = []
    params: list[Any] = []

    for column in _CONTEXT_FIELDS:
        value = context[column]
        if value is None:
            continue
        conflict_terms.append(
            f"CASE WHEN m.{column} IS NOT NULL AND m.{column} <> ? THEN 1 ELSE 0 END"
        )
        params.append(value)

    conflict_param_count = len(params)

    for column in _CONTEXT_FIELDS:
        value = context[column]
        if value is None:
            continue
        match_terms.append(
            f"CASE WHEN m.{column} = ? THEN 1 ELSE 0 END"
        )
        params.append(value)

    conflicts = (
        " + ".join(conflict_terms)
        if conflict_terms
        else "CASE WHEN 1 THEN 0 END"
    )
    matches = (
        " + ".join(match_terms)
        if match_terms
        else "CASE WHEN 1 THEN 0 END"
    )

    # Keep the explicit variable so the parameter order is obvious:
    # conflict placeholders first, then match placeholders.
    assert conflict_param_count <= len(params)
    return f"({conflicts}) ASC, ({matches}) DESC", params


def _partition_context(
    memories: Iterable[Memory],
    context: dict[str, str | None],
) -> tuple[list[Memory], list[Memory]]:
    preferred: list[Memory] = []
    fallback: list[Memory] = []

    for memory in memories:
        target = fallback if _context_conflicts(memory, context) else preferred
        target.append(memory)

    return preferred, fallback


def _context_conflicts(
    memory: Memory,
    context: dict[str, str | None],
) -> bool:
    return any(
        context[column] is not None
        and getattr(memory, column) is not None
        and getattr(memory, column) != context[column]
        for column in _CONTEXT_FIELDS
    )


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _extract_tokens(query: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in _TOKEN_RE.finditer(query)))


def _cjk_ngrams(query: str, size: int) -> list[str]:
    return list(dict.fromkeys(
        run[i:i + size]
        for match in _HAN_RUN_RE.finditer(query)
        for run in (match.group(0),)
        for i in range(len(run) - size + 1)
    ))


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
