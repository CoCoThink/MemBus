from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from membus import Database
from membus.migrations import CURRENT_SCHEMA_VERSION, current_version


def test_fresh_database_migrates_to_current_schema(tmp_path: Path) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()

    with db.connection() as conn:
        assert current_version(conn) == CURRENT_SCHEMA_VERSION
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).casefold() == "wal"


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()
    db.initialize()

    with db.connection() as conn:
        assert current_version(conn) == CURRENT_SCHEMA_VERSION


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
