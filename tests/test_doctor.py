from __future__ import annotations

import json
from pathlib import Path

from membus import MemBus
from membus.cli import main
from membus.doctor import run_doctor


def test_doctor_reports_healthy_database(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    bus = MemBus(path)
    bus.put(
        title="Doctor test",
        content="MemBus doctor checks canonical and FTS consistency.",
        memory_type="fact",
    )

    report = run_doctor(bus)

    assert report["ok"] is True
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["journal_mode"]["ok"] is True
    assert checks["fts5"]["ok"] is True
    assert checks["integrity"]["ok"] is True
    assert checks["fts_consistency"]["ok"] is True
    assert checks["trigram_consistency"]["ok"] is True
    assert checks["supersession"]["ok"] is True
    assert checks["write_transaction"]["ok"] is True


def _drift_report(tmp_path: Path, table: str) -> dict:
    bus = MemBus(tmp_path / "memory.db")
    bus.put(content="This row should also exist in both indexes.", memory_type="fact")
    with bus._transaction() as conn:
        conn.execute(f"DELETE FROM {table}")
    return {c["name"]: c for c in run_doctor(bus)["checks"]}


def test_doctor_detects_word_index_drift(tmp_path: Path) -> None:
    checks = _drift_report(tmp_path, "memory_fts")
    assert checks["fts_consistency"]["ok"] is False
    assert checks["fts_consistency"]["data"]["missing"] == 1
    assert checks["trigram_consistency"]["ok"] is True


def test_doctor_detects_trigram_index_drift(tmp_path: Path) -> None:
    checks = _drift_report(tmp_path, "memory_trigram")
    assert checks["trigram_consistency"]["ok"] is False
    assert checks["trigram_consistency"]["data"]["missing"] == 1
    assert checks["fts_consistency"]["ok"] is True


def test_doctor_detects_orphaned_index_rows(tmp_path: Path) -> None:
    bus = MemBus(tmp_path / "memory.db")
    memory = bus.put(content="soon superseded", memory_type="fact").memory
    with bus._transaction() as conn:
        conn.execute("DROP TRIGGER memories_au")
        conn.execute("UPDATE memories SET deleted_at = 'x' WHERE id = ?", (memory.id,))
    checks = {c["name"]: c for c in run_doctor(bus)["checks"]}
    assert checks["fts_consistency"]["data"]["orphaned"] == 1
    assert checks["trigram_consistency"]["data"]["orphaned"] == 1


def test_doctor_detects_dangling_supersession(tmp_path: Path) -> None:
    bus = MemBus(tmp_path / "memory.db")
    memory = bus.put(content="points nowhere", memory_type="fact").memory
    with bus._transaction() as conn:
        conn.execute("UPDATE memories SET superseded_by = 'mem_ghost' WHERE id = ?",
                     (memory.id,))
    checks = {c["name"]: c for c in run_doctor(bus)["checks"]}
    assert checks["supersession"]["ok"] is False
    assert checks["supersession"]["data"]["dangling"] == 1


def test_cli_doctor_uses_exit_code_and_json(tmp_path: Path, capsys) -> None:
    path = tmp_path / "memory.db"

    exit_code = main(["--db", str(path), "doctor"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["database"] == str(path)


def test_doctor_counts_active_memories_only(tmp_path: Path) -> None:
    bus = MemBus(tmp_path / "db.sqlite")
    deleted = bus.put(content="soft deleted", memory_type="note").memory
    old = bus.put(content="replaced", memory_type="note").memory
    bus.put(content="replacement", memory_type="note", supersedes=[old.id])
    assert bus.delete(deleted.id)

    report = run_doctor(bus)
    assert report["ok"] is True
    for name in ("fts_consistency", "trigram_consistency"):
        data = next(x for x in report["checks"] if x["name"] == name)["data"]
        assert data["memory_count"] == data["fts_count"] == 1
