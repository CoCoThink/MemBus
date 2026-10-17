from __future__ import annotations

from pathlib import Path

from membus import Database


def test_initialize_creates_current_schema_and_wal(tmp_path: Path) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()

    with db.connection() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).casefold() == "wal"

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
    db = Database(tmp_path / "memory.db")
    db.initialize()
    db.initialize()

    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
