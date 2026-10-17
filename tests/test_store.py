from __future__ import annotations

from pathlib import Path

from membus import SQLiteStore


def test_initialize_creates_schema_and_wal(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "memory.db")
    store.initialize()

    with store.connection() as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold() == "wal"
        names = {
            str(row["name"])
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE name IN ('memories', 'aliases', 'memory_fts')
                """
            )
        }
        assert names == {"memories", "aliases", "memory_fts"}


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "memory.db")
    store.initialize()
    store.initialize()

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
