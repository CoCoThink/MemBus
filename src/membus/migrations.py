"""Schema initialization and migrations for MemBus."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


CURRENT_SCHEMA_VERSION = 2
Migration = Callable[[sqlite3.Connection], None]


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'schema_meta'
        """
    ).fetchone()
    if row is None:
        return 0

    version_row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if version_row is None:
        raise RuntimeError("schema_meta exists without schema_version")
    return int(version_row[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Bring a database up to CURRENT_SCHEMA_VERSION.

    Fresh databases are created directly at the current schema. Existing
    databases use ordered migrations. A newer database is rejected.
    """

    version = current_version(conn)
    if version > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema {version} is newer than supported "
            f"version {CURRENT_SCHEMA_VERSION}"
        )

    if version == 0:
        _create_current_schema(conn)
        version = current_version(conn)
        if version != CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                "Fresh schema initialization did not set the expected "
                f"schema_version={CURRENT_SCHEMA_VERSION}; found {version}"
            )
        return version

    while version < CURRENT_SCHEMA_VERSION:
        target = version + 1
        migration = MIGRATIONS.get(target)
        if migration is None:
            raise RuntimeError(
                f"No migration registered for schema {version} -> {target}"
            )
        migration(conn)
        version = current_version(conn)
        if version != target:
            raise RuntimeError(
                f"Migration to schema {target} completed without setting "
                f"schema_version={target}; found {version}"
            )

    return version


def _create_current_schema(conn: sqlite3.Connection) -> None:
    schema_path = Path(__file__).with_name("schema.sql")
    schema = schema_path.read_text(encoding="utf-8")
    try:
        conn.executescript(schema)
    except sqlite3.OperationalError as exc:
        if "fts5" in str(exc).lower():
            raise RuntimeError(
                "This SQLite build does not provide FTS5, which MemBus requires."
            ) from exc
        raise


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Shrink the canonical schema to fields with current runtime semantics."""

    invalid_statuses = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT DISTINCT status
            FROM memories
            WHERE status NOT IN ('active', 'deleted')
            """
        ).fetchall()
    ]
    if invalid_statuses:
        raise RuntimeError(
            "Cannot migrate schema v1 with unknown memory status values: "
            + ", ".join(sorted(invalid_statuses))
        )

    foreign_keys_row = conn.execute("PRAGMA foreign_keys").fetchone()
    foreign_keys_enabled = bool(foreign_keys_row and int(foreign_keys_row[0]) == 1)

    # SQLite cannot toggle foreign_keys inside a transaction. Temporarily turn
    # enforcement off while rebuilding the referenced memories table, then run
    # foreign_key_check before returning.
    if foreign_keys_enabled:
        conn.execute("PRAGMA foreign_keys = OFF")

    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _record_removed_v1_metadata(conn)

            conn.execute(
                """
                CREATE TABLE memories_v2 (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    workspace TEXT,
                    project TEXT,
                    repo TEXT,
                    branch TEXT,
                    source_type TEXT,
                    source_ref TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO memories_v2 (
                    id, title, content, memory_type, scope,
                    workspace, project, repo, branch,
                    source_type, source_ref,
                    created_at, updated_at, deleted_at
                )
                SELECT
                    id, title, content, memory_type, scope,
                    workspace, project, repo, branch,
                    source_type, source_ref,
                    created_at, updated_at,
                    CASE
                        WHEN status = 'deleted'
                            THEN COALESCE(deleted_at, updated_at)
                        ELSE NULL
                    END
                FROM memories
                """
            )

            conn.execute("DROP TRIGGER IF EXISTS memories_ai")
            conn.execute("DROP TRIGGER IF EXISTS memories_au")
            conn.execute("DROP TRIGGER IF EXISTS memories_ad")
            conn.execute("DROP TABLE memories")
            conn.execute("ALTER TABLE memories_v2 RENAME TO memories")

            conn.execute(
                """
                CREATE TRIGGER memories_ai
                AFTER INSERT ON memories BEGIN
                    INSERT INTO memory_fts(memory_id, title, content)
                    VALUES (new.id, new.title, new.content);
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER memories_au
                AFTER UPDATE OF title, content ON memories BEGIN
                    DELETE FROM memory_fts WHERE memory_id = old.id;
                    INSERT INTO memory_fts(memory_id, title, content)
                    VALUES (new.id, new.title, new.content);
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER memories_ad
                AFTER DELETE ON memories BEGIN
                    DELETE FROM memory_fts WHERE memory_id = old.id;
                END
                """
            )

            # FTS is derived state. Rebuild it from the new canonical table.
            conn.execute("DELETE FROM memory_fts")
            conn.execute(
                """
                INSERT INTO memory_fts(memory_id, title, content)
                SELECT id, title, content FROM memories
                """
            )

            conn.execute(
                """
                CREATE TABLE retrieval_logs_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT,
                    scope_json TEXT,
                    candidate_count INTEGER NOT NULL,
                    result_ids_json TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO retrieval_logs_v2(
                    id, query, scope_json, candidate_count,
                    result_ids_json, latency_ms, created_at
                )
                SELECT
                    id, query, scope_json, candidate_count,
                    result_ids_json, latency_ms, created_at
                FROM retrieval_logs
                """
            )
            conn.execute("DROP TABLE retrieval_logs")
            conn.execute("ALTER TABLE retrieval_logs_v2 RENAME TO retrieval_logs")

            conn.execute(
                """
                UPDATE schema_meta
                SET value = '2'
                WHERE key = 'schema_version'
                """
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
    finally:
        if foreign_keys_enabled:
            conn.execute("PRAGMA foreign_keys = ON")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            f"Schema v2 migration produced {len(violations)} foreign-key violation(s)"
        )


def _record_removed_v1_metadata(conn: sqlite3.Connection) -> None:
    """Preserve non-default removed metadata in the existing audit ledger."""

    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        """
        SELECT
            id, importance, confidence, trust,
            valid_from, valid_until, supersedes_id
        FROM memories
        """
    ).fetchall()

    for row in rows:
        removed: dict[str, object] = {}
        if float(row[1]) != 0.5:
            removed["importance"] = float(row[1])
        if float(row[2]) != 1.0:
            removed["confidence"] = float(row[2])
        if float(row[3]) != 0.5:
            removed["trust"] = float(row[3])
        if row[4] is not None:
            removed["valid_from"] = row[4]
        if row[5] is not None:
            removed["valid_until"] = row[5]
        if row[6] is not None:
            removed["supersedes_id"] = row[6]

        if not removed:
            continue

        conn.execute(
            """
            INSERT INTO memory_events(
                memory_id, event_type, payload_json, actor, created_at
            ) VALUES (?, 'schema_v2_removed_metadata', ?, 'schema-migration', ?)
            """,
            (
                str(row[0]),
                json.dumps({"removed": removed}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )


MIGRATIONS: dict[int, Migration] = {
    2: _migrate_to_v2,
}
