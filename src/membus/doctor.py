"""Application and data health checks for MemBus."""

from __future__ import annotations

from typing import Any, Callable

from .store import SQLiteStore


def run_doctor(store: SQLiteStore) -> dict[str, Any]:
    try:
        store.initialize()
    except Exception as exc:  # noqa: BLE001
        return {
            "database": str(store.path),
            "ok": False,
            "checks": [
                {
                    "name": "initialize",
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            ],
        }

    checks: list[dict[str, Any]] = [
        {
            "name": "initialize",
            "ok": True,
            "detail": "database opened and current schema initialized",
        }
    ]

    probes: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
        ("journal_mode", lambda: _check_wal(store)),
        ("fts5", lambda: _check_fts5(store)),
        ("integrity", lambda: _check_integrity(store)),
        ("fts_consistency", lambda: _check_fts_consistency(store)),
        ("write_transaction", lambda: _check_write_transaction(store)),
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
        "database": str(store.path),
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
    }


def _check_wal(store: SQLiteStore) -> dict[str, Any]:
    with store.connection() as conn:
        row = conn.execute("PRAGMA journal_mode").fetchone()
    mode = str(row[0]).casefold() if row is not None else "unknown"
    return {
        "name": "journal_mode",
        "ok": mode == "wal",
        "detail": f"journal_mode={mode}",
    }


def _check_fts5(store: SQLiteStore) -> dict[str, Any]:
    with store.connection() as conn:
        conn.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE memory_fts MATCH ?",
            ('"membus-doctor-token"',),
        ).fetchone()
    return {
        "name": "fts5",
        "ok": True,
        "detail": "FTS5 MATCH query succeeded",
    }


def _check_integrity(store: SQLiteStore) -> dict[str, Any]:
    with store.connection() as conn:
        messages = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    ok = messages == ["ok"]
    return {
        "name": "integrity",
        "ok": ok,
        "detail": "ok" if ok else "; ".join(messages[:10]),
    }


def _check_fts_consistency(store: SQLiteStore) -> dict[str, Any]:
    with store.connection() as conn:
        memory_count = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        fts_count = int(conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0])
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


def _check_write_transaction(store: SQLiteStore) -> dict[str, Any]:
    with store.transaction() as conn:
        conn.execute("UPDATE aliases SET canonical = canonical WHERE 0")
    return {
        "name": "write_transaction",
        "ok": True,
        "detail": "BEGIN IMMEDIATE write probe succeeded",
    }
