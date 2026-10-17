"""SQLite storage for MemBus."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Iterator

from .models import Memory, SearchContext


_MEMORY_COLUMNS = (
    "id",
    "title",
    "content",
    "memory_type",
    "workspace",
    "project",
    "repo",
    "branch",
    "source_type",
    "source_ref",
    "created_at",
    "updated_at",
)


class SQLiteStore:
    """Own the canonical SQLite database and its derived FTS index."""

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

    def connect(self) -> sqlite3.Connection:
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

    def initialize(self) -> None:
        with contextlib.closing(self.connect()) as conn:
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

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
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

    def backup(self, destination: str | Path) -> Path:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        with contextlib.closing(self.connect()) as source:
            with contextlib.closing(sqlite3.connect(destination_path)) as target:
                source.backup(target)

        return destination_path

    def create(self, memory: Memory) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO memories (
                    id, title, content, memory_type,
                    workspace, project, repo, branch,
                    source_type, source_ref,
                    created_at, updated_at
                ) VALUES (
                    :id, :title, :content, :memory_type,
                    :workspace, :project, :repo, :branch,
                    :source_type, :source_ref,
                    :created_at, :updated_at
                )
                """,
                _memory_params(memory),
            )

    def get(self, memory_id: str) -> Memory | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
        return _memory_from_row(row) if row is not None else None

    def update(self, memory: Memory) -> Memory:
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE memories
                SET
                    title = :title,
                    content = :content,
                    memory_type = :memory_type,
                    workspace = :workspace,
                    project = :project,
                    repo = :repo,
                    branch = :branch,
                    source_type = :source_type,
                    source_ref = :source_ref,
                    updated_at = :updated_at
                WHERE id = :id
                """,
                _memory_params(memory),
            )
            if cursor.rowcount == 0:
                raise KeyError(memory.id)
        return memory

    def delete(self, memory_id: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE id = ?",
                (memory_id,),
            )
            return cursor.rowcount > 0

    def exact_candidates(
        self,
        query: str,
        *,
        context: SearchContext,
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
        _append_context_filter(where, params, context)
        _append_type_filter(where, params, types)

        sql = f"""
            SELECT m.*
            FROM memories AS m
            WHERE {' AND '.join(where)}
            ORDER BY m.id ASC
            LIMIT ?
        """
        params.append(limit)

        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_memory_from_row(row) for row in rows]

    def fts_candidates(
        self,
        match_query: str,
        *,
        context: SearchContext,
        types: tuple[str, ...],
        limit: int,
    ) -> list[Memory]:
        where = ["memory_fts MATCH ?"]
        params: list[Any] = [match_query]
        _append_context_filter(where, params, context)
        _append_type_filter(where, params, types)

        sql = f"""
            SELECT m.*
            FROM memory_fts
            JOIN memories AS m ON m.id = memory_fts.memory_id
            WHERE {' AND '.join(where)}
            ORDER BY bm25(memory_fts) ASC, m.id ASC
            LIMIT ?
        """
        params.append(limit)

        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_memory_from_row(row) for row in rows]

    def alias_map(self, tokens: Iterable[str]) -> dict[str, str]:
        normalized = tuple(dict.fromkeys(t.casefold() for t in tokens if t))
        if not normalized:
            return {}

        placeholders = ",".join("?" for _ in normalized)
        sql = f"""
            SELECT alias, canonical
            FROM aliases
            WHERE alias IN ({placeholders})
        """

        with self.connection() as conn:
            rows = conn.execute(sql, normalized).fetchall()

        return {
            str(row["alias"]).casefold(): str(row["canonical"]).casefold()
            for row in rows
        }

    def put_alias(self, alias: str, canonical: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO aliases(alias, canonical)
                VALUES (?, ?)
                ON CONFLICT(alias) DO UPDATE SET canonical = excluded.canonical
                """,
                (alias.casefold(), canonical.casefold()),
            )

    def _ensure_wal(self, conn: sqlite3.Connection) -> None:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(self.begin_retry_attempts):
            try:
                row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
                if row is None or str(row[0]).casefold() != "wal":
                    raise RuntimeError("SQLite refused WAL journal mode")
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

    def _begin_with_retry(self, conn: sqlite3.Connection) -> None:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(self.begin_retry_attempts):
            try:
                conn.execute("BEGIN IMMEDIATE")
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


def _append_context_filter(
    where: list[str],
    params: list[Any],
    context: SearchContext,
) -> None:
    for column in ("workspace", "project", "repo", "branch"):
        value = getattr(context, column)
        if value is not None:
            where.append(f"(m.{column} IS NULL OR m.{column} = ?)")
            params.append(value)


def _append_type_filter(
    where: list[str],
    params: list[Any],
    types: tuple[str, ...],
) -> None:
    if not types:
        return
    placeholders = ",".join("?" for _ in types)
    where.append(f"m.memory_type IN ({placeholders})")
    params.extend(types)


def _memory_params(memory: Memory) -> dict[str, Any]:
    return {column: getattr(memory, column) for column in _MEMORY_COLUMNS}


def _memory_from_row(row: sqlite3.Row) -> Memory:
    return Memory(**{column: row[column] for column in _MEMORY_COLUMNS})


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message
