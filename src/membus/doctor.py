"""Application and data health checks for MemBus."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from .db import Database
from .migrations import CURRENT_SCHEMA_VERSION, current_version


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str
    data: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DoctorReport:
    database: str
    ok: bool
    checks: tuple[DoctorCheck, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "ok": self.ok,
            "checks": [check.as_dict() for check in self.checks],
        }


def run_doctor(db: Database) -> DoctorReport:
    """Run bounded MemBus readiness checks.

    The checks intentionally cover only the application/data layer. Tunnel
    transport health belongs to tunnel-client doctor.
    """

    checks: list[DoctorCheck] = []

    try:
        db.initialize()
    except Exception as exc:  # noqa: BLE001 - doctor must report, not crash
        checks.append(
            DoctorCheck(
                name="initialize",
                ok=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
        )
        return DoctorReport(
            database=str(db.path),
            ok=False,
            checks=tuple(checks),
        )

    checks.append(
        DoctorCheck(
            name="initialize",
            ok=True,
            detail="database opened and schema initialization completed",
        )
    )

    probes: tuple[tuple[str, Callable[[], DoctorCheck]], ...] = (
        ("schema", lambda: _check_schema(db)),
        ("journal_mode", lambda: _check_wal(db)),
        ("foreign_keys", lambda: _check_foreign_keys(db)),
        ("fts5", lambda: _check_fts5(db)),
        ("integrity", lambda: _check_integrity(db)),
        ("fts_consistency", lambda: _check_fts_consistency(db)),
        ("write_transaction", lambda: _check_write_transaction(db)),
    )

    for name, probe in probes:
        try:
            checks.append(probe())
        except Exception as exc:  # noqa: BLE001 - convert failures to report
            checks.append(
                DoctorCheck(
                    name=name,
                    ok=False,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

    return DoctorReport(
        database=str(db.path),
        ok=all(check.ok for check in checks),
        checks=tuple(checks),
    )


def _check_schema(db: Database) -> DoctorCheck:
    with db.connection() as conn:
        version = current_version(conn)

    ok = version == CURRENT_SCHEMA_VERSION
    return DoctorCheck(
        name="schema",
        ok=ok,
        detail=(
            f"schema version {version}"
            if ok
            else f"schema version {version}; expected {CURRENT_SCHEMA_VERSION}"
        ),
        data={
            "version": version,
            "supported_version": CURRENT_SCHEMA_VERSION,
        },
    )


def _check_wal(db: Database) -> DoctorCheck:
    with db.connection() as conn:
        row = conn.execute("PRAGMA journal_mode").fetchone()
    mode = str(row[0]).casefold() if row is not None else "unknown"
    return DoctorCheck(
        name="journal_mode",
        ok=mode == "wal",
        detail=f"journal_mode={mode}",
        data={"journal_mode": mode},
    )


def _check_foreign_keys(db: Database) -> DoctorCheck:
    with db.connection() as conn:
        row = conn.execute("PRAGMA foreign_keys").fetchone()
    enabled = bool(row and int(row[0]) == 1)
    return DoctorCheck(
        name="foreign_keys",
        ok=enabled,
        detail=f"foreign_keys={'on' if enabled else 'off'}",
    )


def _check_fts5(db: Database) -> DoctorCheck:
    with db.connection() as conn:
        # Query the actual MemBus FTS table instead of relying only on SQLite
        # compile options. This verifies that MATCH works in the deployed DB.
        conn.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE memory_fts MATCH ?",
            ('"membus-doctor-token"',),
        ).fetchone()

    return DoctorCheck(
        name="fts5",
        ok=True,
        detail="FTS5 MATCH query succeeded",
    )


def _check_integrity(db: Database) -> DoctorCheck:
    with db.connection() as conn:
        rows = conn.execute("PRAGMA integrity_check").fetchall()

    messages = [str(row[0]) for row in rows]
    ok = messages == ["ok"]
    return DoctorCheck(
        name="integrity",
        ok=ok,
        detail="ok" if ok else "; ".join(messages[:10]),
        data={"messages": messages[:10]},
    )


def _check_fts_consistency(db: Database) -> DoctorCheck:
    with db.connection() as conn:
        memory_count = int(
            conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        )
        fts_count = int(
            conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
        )
        missing = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM memories AS m
                LEFT JOIN memory_fts AS f ON f.memory_id = m.id
                WHERE f.memory_id IS NULL
                """
            ).fetchone()[0]
        )
        orphaned = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM memory_fts AS f
                LEFT JOIN memories AS m ON m.id = f.memory_id
                WHERE m.id IS NULL
                """
            ).fetchone()[0]
        )

    ok = (
        memory_count == fts_count
        and missing == 0
        and orphaned == 0
    )
    return DoctorCheck(
        name="fts_consistency",
        ok=ok,
        detail=(
            "canonical and FTS rows are synchronized"
            if ok
            else (
                f"memory_count={memory_count}, fts_count={fts_count}, "
                f"missing={missing}, orphaned={orphaned}"
            )
        ),
        data={
            "memory_count": memory_count,
            "fts_count": fts_count,
            "missing": missing,
            "orphaned": orphaned,
        },
    )


def _check_write_transaction(db: Database) -> DoctorCheck:
    # Perform a no-op canonical write inside the normal writer path. This
    # verifies BEGIN IMMEDIATE and main-database writability without leaving
    # application data behind.
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE schema_meta
            SET value = value
            WHERE key = 'schema_version'
            """
        )
        if cursor.rowcount not in (-1, 1):
            raise RuntimeError(
                f"schema_version write probe affected {cursor.rowcount} rows"
            )

    return DoctorCheck(
        name="write_transaction",
        ok=True,
        detail="BEGIN IMMEDIATE write probe succeeded",
    )
