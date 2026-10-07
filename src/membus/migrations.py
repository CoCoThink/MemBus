"""Schema migration framework for MemBus.

V1 is intentionally the only schema version today. This module exists now so
future schema changes have an explicit compatibility path instead of growing
ad-hoc startup DDL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable


CURRENT_SCHEMA_VERSION = 1
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

    A database newer than this binary is rejected. Each future migration must be
    registered in MIGRATIONS and must update schema_meta only after its changes
    succeed.
    """

    version = current_version(conn)
    if version > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema {version} is newer than supported "
            f"version {CURRENT_SCHEMA_VERSION}"
        )

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


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    schema_path = Path(__file__).with_name("schema.sql")
    schema = schema_path.read_text(encoding="utf-8")
    try:
        conn.executescript(schema)
    except sqlite3.OperationalError as exc:
        if "fts5" in str(exc).lower():
            raise RuntimeError(
                "This SQLite build does not provide FTS5, which MemBus V1 requires."
            ) from exc
        raise


MIGRATIONS: dict[int, Migration] = {
    1: _migrate_to_v1,
}
