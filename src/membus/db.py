"""SQLite infrastructure for MemBus."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Iterator


class Database:
    """Owns SQLite connections, initialization, write transactions, and backups."""

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
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open the single application write transaction shape: BEGIN IMMEDIATE."""

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

    def _begin_with_retry(self, conn: sqlite3.Connection) -> None:
        last_error: sqlite3.OperationalError | None = None

        for attempt in range(self.begin_retry_attempts):
            try:
                conn.execute("BEGIN IMMEDIATE")
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
