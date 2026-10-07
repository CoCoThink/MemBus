"""SQLite infrastructure for MemBus."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Iterator


SUPPORTED_SCHEMA_VERSION = 1


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
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def initialize(self) -> None:
        schema_path = Path(__file__).with_name("schema.sql")
        schema = schema_path.read_text(encoding="utf-8")

        with contextlib.closing(self.connect()) as conn:
            try:
                conn.executescript(schema)
            except sqlite3.OperationalError as exc:
                if "fts5" in str(exc).lower():
                    raise RuntimeError(
                        "This SQLite build does not provide FTS5, which MemBus V1 requires."
                    ) from exc
                raise

            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                raise RuntimeError("MemBus schema exists without schema_version metadata")

            version = int(row["value"])
            if version > SUPPORTED_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema {version} is newer than supported "
                    f"version {SUPPORTED_SCHEMA_VERSION}"
                )
            if version < SUPPORTED_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema {version} requires a migration to "
                    f"{SUPPORTED_SCHEMA_VERSION}; migrations are not yet available"
                )

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
        """Create a transactionally consistent SQLite backup.

        This intentionally uses sqlite3.Connection.backup() rather than copying
        only the main database file while WAL mode may contain uncheckpointed data.
        """

        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        with contextlib.closing(self.connect()) as source:
            with contextlib.closing(sqlite3.connect(destination_path)) as target:
                source.backup(target)

        return destination_path
