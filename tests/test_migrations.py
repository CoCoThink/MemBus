from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from membus import Database, MemoryService
from membus.migrations import CURRENT_SCHEMA_VERSION, current_version


EXPECTED_MEMORY_COLUMNS = {
    "id",
    "title",
    "content",
    "memory_type",
    "scope",
    "workspace",
    "project",
    "repo",
    "branch",
    "source_type",
    "source_ref",
    "created_at",
    "updated_at",
    "deleted_at",
}


def test_fresh_database_migrates_to_current_schema(tmp_path: Path) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()

    with db.connection() as conn:
        assert current_version(conn) == CURRENT_SCHEMA_VERSION == 2
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).casefold() == "wal"

        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(memories)")
        }
        assert columns == EXPECTED_MEMORY_COLUMNS


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()
    db.initialize()

    with db.connection() as conn:
        assert current_version(conn) == CURRENT_SCHEMA_VERSION


def test_v1_database_migrates_to_compact_v2(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _create_v1_database(path)

    db = Database(path)
    db.initialize()

    with db.connection() as conn:
        assert current_version(conn) == 2

        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(memories)")
        }
        assert columns == EXPECTED_MEMORY_COLUMNS

        memory = conn.execute(
            "SELECT * FROM memories WHERE id = 'mem_legacy'"
        ).fetchone()
        assert memory is not None
        assert memory["content"] == "Legacy canonical content survives migration."
        assert memory["deleted_at"] == "2026-01-02T00:00:00+00:00"

        log_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(retrieval_logs)")
        }
        assert "debug_json" not in log_columns

        audit = conn.execute(
            """
            SELECT payload_json
            FROM memory_events
            WHERE memory_id = 'mem_legacy'
              AND event_type = 'schema_v2_removed_metadata'
            """
        ).fetchone()
        assert audit is not None
        removed = json.loads(audit["payload_json"])["removed"]
        assert removed == {
            "importance": 0.9,
            "trust": 0.8,
            "valid_from": "2025-01-01T00:00:00+00:00",
        }

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    service = MemoryService(db)
    assert service.get("mem_legacy") is None
    legacy = service.get("mem_legacy", include_deleted=True)
    assert legacy is not None
    assert legacy.content == "Legacy canonical content survives migration."


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    db = Database(path)
    db.initialize()

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            (str(CURRENT_SCHEMA_VERSION + 1),),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="newer than supported"):
        db.initialize()


def test_missing_required_table_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    db = Database(path)
    db.initialize()

    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER IF EXISTS memories_ai")
        conn.execute("DROP TRIGGER IF EXISTS memories_au")
        conn.execute("DROP TRIGGER IF EXISTS memories_ad")
        conn.execute("DROP TABLE memory_fts")
        conn.commit()

    with pytest.raises(RuntimeError, match="schema is incomplete"):
        db.initialize()


def _create_v1_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value)
            VALUES ('schema_version', '1');

            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                workspace TEXT,
                project TEXT,
                repo TEXT,
                branch TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                importance REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 1.0,
                trust REAL NOT NULL DEFAULT 0.5,
                source_type TEXT,
                source_ref TEXT,
                valid_from TEXT,
                valid_until TEXT,
                supersedes_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT,
                FOREIGN KEY (supersedes_id) REFERENCES memories(id)
            );

            CREATE TABLE aliases (
                alias TEXT PRIMARY KEY,
                canonical TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT,
                actor TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(memory_id) REFERENCES memories(id)
            );

            CREATE TABLE retrieval_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT,
                scope_json TEXT,
                candidate_count INTEGER NOT NULL,
                result_ids_json TEXT NOT NULL,
                latency_ms REAL NOT NULL,
                debug_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE memory_fts USING fts5(
                memory_id UNINDEXED,
                title,
                content,
                tokenize = 'unicode61 remove_diacritics 2'
            );

            CREATE TRIGGER memories_ai
            AFTER INSERT ON memories BEGIN
                INSERT INTO memory_fts(memory_id, title, content)
                VALUES (new.id, new.title, new.content);
            END;

            CREATE TRIGGER memories_au
            AFTER UPDATE OF title, content ON memories BEGIN
                DELETE FROM memory_fts WHERE memory_id = old.id;
                INSERT INTO memory_fts(memory_id, title, content)
                VALUES (new.id, new.title, new.content);
            END;

            CREATE TRIGGER memories_ad
            AFTER DELETE ON memories BEGIN
                DELETE FROM memory_fts WHERE memory_id = old.id;
            END;
            """
        )

        conn.execute(
            """
            INSERT INTO memories(
                id, title, content, memory_type, scope,
                workspace, project, repo, branch, status,
                importance, confidence, trust,
                source_type, source_ref,
                valid_from, valid_until, supersedes_id,
                created_at, updated_at, deleted_at
            ) VALUES (
                'mem_legacy', 'Legacy', 'Legacy canonical content survives migration.',
                'fact', 'global',
                NULL, NULL, NULL, NULL, 'deleted',
                0.9, 1.0, 0.8,
                'test', 'legacy-fixture',
                '2025-01-01T00:00:00+00:00', NULL, NULL,
                '2026-01-01T00:00:00+00:00',
                '2026-01-02T00:00:00+00:00',
                NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO memory_events(
                memory_id, event_type, payload_json, actor, created_at
            ) VALUES (
                'mem_legacy', 'created', NULL, 'fixture',
                '2026-01-01T00:00:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO retrieval_logs(
                query, scope_json, candidate_count,
                result_ids_json, latency_ms, debug_json, created_at
            ) VALUES (
                NULL, '{}', 1, '["mem_legacy"]', 1.0, '{"limit": 10}',
                '2026-01-02T00:00:00+00:00'
            )
            """
        )
        conn.commit()
