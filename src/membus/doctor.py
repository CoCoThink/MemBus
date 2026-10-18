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
        ("fts_consistency", lambda: _check_index(bus, "fts_consistency", "memory_fts")),
        ("trigram_consistency", lambda: _check_index(bus, "trigram_consistency", "memory_trigram")),
        ("supersession", lambda: _check_supersession(bus)),
        ("revisions", lambda: _check_revisions(bus)),
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
        conn.execute(
            "SELECT COUNT(*) FROM memory_trigram WHERE memory_trigram MATCH ?",
            ('"doctor"',),
        ).fetchone()
    return {
        "name": "fts5",
        "ok": True,
        "detail": "FTS5 word and trigram MATCH queries succeeded",
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


def _check_index(bus: MemBus, name: str, table: str) -> dict[str, Any]:
    """An FTS table must hold exactly the active memories, keyed by rid."""
    active = "m.deleted_at IS NULL AND m.superseded_by IS NULL"
    with bus._connection() as conn:
        memory_count = int(conn.execute(
            f"SELECT COUNT(*) FROM memories AS m WHERE {active}"
        ).fetchone()[0])
        fts_count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        missing = int(conn.execute(
            f"""
            SELECT COUNT(*) FROM memories AS m
            LEFT JOIN {table} AS f ON f.rowid = m.rid
            WHERE {active} AND f.rowid IS NULL
            """
        ).fetchone()[0])
        orphaned = int(conn.execute(
            f"""
            SELECT COUNT(*) FROM {table} AS f
            LEFT JOIN memories AS m ON m.rid = f.rowid
            WHERE m.rid IS NULL
               OR m.deleted_at IS NOT NULL OR m.superseded_by IS NOT NULL
            """
        ).fetchone()[0])

    ok = memory_count == fts_count and missing == 0 and orphaned == 0
    return {
        "name": name,
        "ok": ok,
        "detail": (
            f"{table} matches active memories"
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


def _check_supersession(bus: MemBus) -> dict[str, Any]:
    with bus._connection() as conn:
        missing = int(conn.execute(
            """
            SELECT COUNT(*) FROM memories AS m
            LEFT JOIN memories AS s ON s.id = m.superseded_by
            WHERE m.superseded_by IS NOT NULL AND s.id IS NULL
            """
        ).fetchone()[0])
        self_links = int(conn.execute(
            "SELECT COUNT(*) FROM memories WHERE superseded_by = id"
        ).fetchone()[0])
        duplicates = int(conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT content_hash FROM memories
                WHERE deleted_at IS NULL AND superseded_by IS NULL
                GROUP BY content_hash HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0])
    # Purge intentionally leaves historical links to an erased successor.
    ok = self_links == 0 and duplicates == 0
    return {
        "name": "supersession",
        "ok": ok,
        "detail": (
            "no self-supersession or active duplicates"
            if ok
            else f"self_links={self_links}, active_duplicates={duplicates}"
        ),
        "data": {"missing_successors": missing, "self_links": self_links,
                 "active_duplicates": duplicates},
    }


def _check_revisions(bus: MemBus) -> dict[str, Any]:
    with bus._connection() as conn:
        row = conn.execute(
            "SELECT revision, generation, (SELECT COALESCE(MAX(revision), 0) FROM memories) AS latest "
            "FROM metadata WHERE singleton = 1"
        ).fetchone()
    ok = row is not None and bool(row["generation"]) and row["revision"] >= row["latest"]
    return {
        "name": "revisions", "ok": ok,
        "detail": "global counter covers all memory revisions" if ok else "invalid revision metadata",
    }


def _check_write_transaction(bus: MemBus) -> dict[str, Any]:
    with bus._transaction() as conn:
        conn.execute("UPDATE aliases SET canonical = canonical WHERE 0")
    return {
        "name": "write_transaction",
        "ok": True,
        "detail": "BEGIN IMMEDIATE write probe succeeded",
    }
