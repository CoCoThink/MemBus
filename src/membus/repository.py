"""Persistence repository for MemBus.

SQL stays in this module so protocol adapters and domain services never depend
on SQLite table details.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from .db import Database
from .models import Memory


_MEMORY_COLUMNS = (
    "id",
    "title",
    "content",
    "memory_type",
    "scope",
    "workspace",
    "project",
    "repo",
    "branch",
    "status",
    "importance",
    "confidence",
    "trust",
    "source_type",
    "source_ref",
    "valid_from",
    "valid_until",
    "supersedes_id",
    "created_at",
    "updated_at",
    "deleted_at",
)


class MemoryRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, memory: Memory, *, actor: str | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO memories (
                    id, title, content, memory_type, scope,
                    workspace, project, repo, branch, status,
                    importance, confidence, trust,
                    source_type, source_ref,
                    valid_from, valid_until, supersedes_id,
                    created_at, updated_at, deleted_at
                ) VALUES (
                    :id, :title, :content, :memory_type, :scope,
                    :workspace, :project, :repo, :branch, :status,
                    :importance, :confidence, :trust,
                    :source_type, :source_ref,
                    :valid_from, :valid_until, :supersedes_id,
                    :created_at, :updated_at, :deleted_at
                )
                """,
                _memory_params(memory),
            )
            self._append_event(
                conn,
                memory_id=memory.id,
                event_type="created",
                payload={"status": memory.status},
                actor=actor,
                created_at=memory.created_at,
            )

    def get(self, memory_id: str, *, include_deleted: bool = False) -> Memory | None:
        sql = "SELECT * FROM memories WHERE id = ?"
        params: list[Any] = [memory_id]
        if not include_deleted:
            sql += " AND status = 'active'"

        with self.db.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return _memory_from_row(row) if row is not None else None

    def update(
        self,
        memory: Memory,
        *,
        changed_fields: Mapping[str, Any],
        actor: str | None = None,
    ) -> Memory:
        with self.db.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT * FROM memories WHERE id = ?",
                (memory.id,),
            ).fetchone()
            if current is None:
                raise KeyError(memory.id)

            conn.execute(
                """
                UPDATE memories
                SET
                    title = :title,
                    content = :content,
                    memory_type = :memory_type,
                    scope = :scope,
                    workspace = :workspace,
                    project = :project,
                    repo = :repo,
                    branch = :branch,
                    status = :status,
                    importance = :importance,
                    confidence = :confidence,
                    trust = :trust,
                    source_type = :source_type,
                    source_ref = :source_ref,
                    valid_from = :valid_from,
                    valid_until = :valid_until,
                    supersedes_id = :supersedes_id,
                    updated_at = :updated_at,
                    deleted_at = :deleted_at
                WHERE id = :id
                """,
                _memory_params(memory),
            )
            self._append_event(
                conn,
                memory_id=memory.id,
                event_type="updated",
                payload={"changed": dict(changed_fields)},
                actor=actor,
                created_at=memory.updated_at,
            )

        return memory

    def logical_delete(
        self,
        memory_id: str,
        *,
        deleted_at: str,
        actor: str | None = None,
    ) -> bool:
        with self.db.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT status FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if current is None:
                return False
            if current["status"] == "deleted":
                return False

            conn.execute(
                """
                UPDATE memories
                SET status = 'deleted',
                    deleted_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (deleted_at, deleted_at, memory_id),
            )
            self._append_event(
                conn,
                memory_id=memory_id,
                event_type="deleted",
                payload={"status": "deleted"},
                actor=actor,
                created_at=deleted_at,
            )
            return True

    def exact_candidates(
        self,
        query: str,
        *,
        types: tuple[str, ...] = (),
        include_deleted: bool = False,
        limit: int = 50,
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

        if not include_deleted:
            where.append("m.status = 'active'")
        _append_type_filter(where, params, types)

        sql = f"""
            SELECT m.*
            FROM memories AS m
            WHERE {' AND '.join(where)}
            ORDER BY m.updated_at DESC
            LIMIT ?
        """
        params.append(limit)

        with self.db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_memory_from_row(row) for row in rows]

    def fts_candidates(
        self,
        match_query: str,
        *,
        types: tuple[str, ...] = (),
        include_deleted: bool = False,
        limit: int = 50,
    ) -> list[tuple[Memory, float]]:
        where = ["memory_fts MATCH ?"]
        params: list[Any] = [match_query]

        if not include_deleted:
            where.append("m.status = 'active'")
        _append_type_filter(where, params, types)

        sql = f"""
            SELECT
                m.*,
                bm25(memory_fts, 0.0, 5.0, 1.0) AS raw_bm25
            FROM memory_fts
            JOIN memories AS m ON m.id = memory_fts.memory_id
            WHERE {' AND '.join(where)}
            ORDER BY raw_bm25 ASC, m.updated_at DESC
            LIMIT ?
        """
        params.append(limit)

        with self.db.connection() as conn:
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError(f"Invalid FTS query generated by MemBus: {match_query!r}") from exc

        return [(_memory_from_row(row), float(row["raw_bm25"])) for row in rows]

    def alias_map(self, tokens: Iterable[str]) -> dict[str, set[str]]:
        normalized = tuple(dict.fromkeys(t.casefold() for t in tokens if t))
        if not normalized:
            return {}

        placeholders = ",".join("?" for _ in normalized)
        sql = f"""
            SELECT alias, canonical
            FROM aliases
            WHERE alias IN ({placeholders})
               OR canonical IN ({placeholders})
        """
        params = [*normalized, *normalized]
        result: dict[str, set[str]] = {token: set() for token in normalized}

        with self.db.connection() as conn:
            for row in conn.execute(sql, params):
                alias = str(row["alias"]).casefold()
                canonical = str(row["canonical"]).casefold()
                if alias in result:
                    result[alias].add(canonical)
                if canonical in result:
                    result[canonical].add(alias)

        return result

    def put_alias(self, alias: str, canonical: str, *, created_at: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO aliases(alias, canonical, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET
                    canonical = excluded.canonical
                """,
                (alias.casefold(), canonical.casefold(), created_at),
            )

    def append_retrieval_log(
        self,
        *,
        query: str | None,
        scope: Mapping[str, Any],
        candidate_count: int,
        result_ids: list[str],
        latency_ms: float,
        debug: Mapping[str, Any] | None,
        created_at: str,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO retrieval_logs(
                    query, scope_json, candidate_count,
                    result_ids_json, latency_ms, debug_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query,
                    json.dumps(scope, ensure_ascii=False, sort_keys=True),
                    candidate_count,
                    json.dumps(result_ids),
                    latency_ms,
                    json.dumps(debug, ensure_ascii=False, sort_keys=True)
                    if debug is not None
                    else None,
                    created_at,
                ),
            )

    def list_events(self, memory_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, memory_id, event_type, payload_json, actor, created_at
                FROM memory_events
                WHERE memory_id = ?
                ORDER BY id ASC
                """,
                (memory_id,),
            ).fetchall()

        return [
            {
                "id": int(row["id"]),
                "memory_id": str(row["memory_id"]),
                "event_type": str(row["event_type"]),
                "payload": json.loads(row["payload_json"])
                if row["payload_json"]
                else None,
                "actor": row["actor"],
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        *,
        memory_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None,
        actor: str | None,
        created_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO memory_events(
                memory_id, event_type, payload_json, actor, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                event_type,
                json.dumps(payload, ensure_ascii=False, sort_keys=True)
                if payload is not None
                else None,
                actor,
                created_at,
            ),
        )


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
