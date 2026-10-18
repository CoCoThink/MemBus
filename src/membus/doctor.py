"""Application and data health checks for MemBus."""

from __future__ import annotations

from typing import Any, Callable

from .core import MemBus


def run_doctor(bus: MemBus) -> dict[str, Any]:
    checks: list[dict[str, Any]] = [
        {
            "name": "initialize",
            "ok": True,
            "detail": "database opened and current schema initialized",
        }
    ]

    probes: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
        ("journal_mode", lambda: _check_wal(bus)),
        ("fts5", lambda: _check_fts5(bus)),
        ("integrity", lambda: _check_integrity(bus)),
        ("fts_consistency", lambda: _check_fts_consistency(bus)),
        ("write_transaction", lambda: _check_write_transaction(bus)),
    )

    for name, probe in probes:
        try:
            checks.append(probe())
        except Exception as exc:  # noqa: BLE001
            checks.append(
                {
                    "name": name,
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )

    return {
        "database": str(bus.path),
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
    }


def _check_wal(bus: MemBus) -> dict[str, Any]:
    with bus._connection() as conn:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
    return {
        "name": "journal_mode",
        "ok": mode == "wal",
        "detail": f"journal_mode={mode}",
    }


def _check_fts5(bus: MemBus) -> dict[str, Any]:
    with bus._connection() as conn:
        conn.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE memory_fts MATCH ?",
            ('"membus-doctor-token"',),
        ).fetchone()
    return {
        "name": "fts5",
        "ok": True,
        "detail": "FTS5 MATCH query succeeded",
    }


def _check_integrity(bus: MemBus) -> dict[str, Any]:
    with bus._connection() as conn:
        messages = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    ok = messages == ["ok"]
    return {
        "name": "integrity",
        "ok": ok,
        "detail": "ok" if ok else "; ".join(messages[:10]),
    }


def _check_fts_consistency(bus: MemBus) -> dict[str, Any]:
    with bus._connection() as conn:
        memory_count = int(conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0])
        fts_count = int(conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0])
        missing = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM memories AS m
                LEFT JOIN memory_fts AS f ON f.memory_id = m.id
                WHERE m.deleted_at IS NULL AND f.memory_id IS NULL
                """
            ).fetchone()[0]
        )
        orphaned = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM memory_fts AS f
                LEFT JOIN memories AS m ON m.id = f.memory_id
                WHERE m.id IS NULL OR m.deleted_at IS NOT NULL
                """
            ).fetchone()[0]
        )

    ok = memory_count == fts_count and missing == 0 and orphaned == 0
    return {
        "name": "fts_consistency",
        "ok": ok,
        "detail": (
            "canonical and FTS rows are synchronized"
            if ok
            else (
                f"memory_count={memory_count}, fts_count={fts_count}, "
                f"missing={missing}, orphaned={orphaned}"
            )
        ),
        "data": {
            "memory_count": memory_count,
            "fts_count": fts_count,
            "missing": missing,
            "orphaned": orphaned,
        },
    }


def _check_write_transaction(bus: MemBus) -> dict[str, Any]:
    with bus._transaction() as conn:
        conn.execute("UPDATE aliases SET canonical = canonical WHERE 0")
    return {
        "name": "write_transaction",
        "ok": True,
        "detail": "BEGIN IMMEDIATE write probe succeeded",
    }
