"""SQLite infrastructure for MemBus."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from .migrations import CURRENT_SCHEMA_VERSION, migrate


class Database:
    """Owns SQLite connections, initialization, transactions, and backups."""

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
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def initialize(self) -> None:
        with contextlib.closing(self.connect()) as conn:
            self._ensure_wal(conn)
            version = migrate(conn)
            if version != CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"MemBus expected schema {CURRENT_SCHEMA_VERSION}, found {version}"
                )

            # Lightweight startup verification: fail early if the canonical
            # table or FTS index disappeared even when schema_meta survived.
            required = {"memories", "memory_fts", "memory_events"}
            rows = conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE name IN ('memories', 'memory_fts', 'memory_events')
                """
            ).fetchall()
            found = {str(row["name"]) for row in rows}
            missing = required - found
            if missing:
                raise RuntimeError(
                    "MemBus schema is incomplete; missing: "
                    + ", ".join(sorted(missing))
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
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                last_error = exc
                if attempt + 1 >= self.begin_retry_attempts:
                    break
                time.sleep(0.02 * (2**attempt))

        assert last_error is not None
        raise last_error

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Open a write transaction.

        BEGIN IMMEDIATE is used for read-modify-write flows that must reserve the
        writer slot before reading state that will be mutated.
        """

        conn = self.connect()
        begin = "BEGIN IMMEDIATE" if immediate else "BEGIN"

        try:
            self._begin_with_retry(conn, begin)
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()

    def _begin_with_retry(self, conn: sqlite3.Connection, begin: str) -> None:
        last_error: sqlite3.OperationalError | None = None

        for attempt in range(self.begin_retry_attempts):
            try:
                conn.execute(begin)
                return
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                last_error = exc
                if attempt + 1 >= self.begin_retry_attempts:
                    break
                time.sleep(0.02 * (2**attempt))

        assert last_error is not None
        raise last_error

    def backup(self, destination: str | Path) -> Path:
        """Create a transactionally consistent SQLite backup."""

        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        with contextlib.closing(self.connect()) as source:
            with contextlib.closing(sqlite3.connect(destination_path)) as target:
                source.backup(target)

        return destination_path
