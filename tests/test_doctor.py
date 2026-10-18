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
    assert checks["write_transaction"]["ok"] is True


def test_doctor_detects_fts_drift(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    bus = MemBus(path)
    memory = bus.put(
        content="This row should also exist in FTS.",
        memory_type="fact",
    )

    with bus._transaction() as conn:
        conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory.id,))

    report = run_doctor(bus)

    assert report["ok"] is False
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["fts_consistency"]["ok"] is False
    assert checks["fts_consistency"]["data"]["missing"] == 1


def test_cli_doctor_uses_exit_code_and_json(tmp_path: Path, capsys) -> None:
    path = tmp_path / "memory.db"

    exit_code = main(["--db", str(path), "doctor"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["database"] == str(path)


def test_doctor_counts_live_memories_only(tmp_path: Path) -> None:
    bus = MemBus(tmp_path / "db.sqlite")
    memory = bus.put(content="soft deleted", memory_type="note")
    assert bus.delete(memory.id)
    report = run_doctor(bus)
    assert report["ok"] is True
    consistency = next(x for x in report["checks"]
                       if x["name"] == "fts_consistency")
    assert consistency["data"]["memory_count"] == 0
    assert consistency["data"]["fts_count"] == 0
