from __future__ import annotations

import json
from pathlib import Path

from membus.cli import main


def _json_output(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_cli_put_search_get_delete_and_backup(tmp_path: Path, capsys) -> None:
    db = tmp_path / "memory.db"

    assert main([
        "--db", str(db),
        "put",
        "--content", "BUG-9001 is caused by payment retry logic.",
        "--title", "BUG-9001",
        "--memory-type", "incident",
        "--source", "cli-agent",
        "--project", "payment",
    ]) == 0
    created = _json_output(capsys)
    memory_id = created["id"]
    assert created["source"] == "cli-agent"

    assert main([
        "--db", str(db),
        "search",
        "BUG-9001",
        "--project", "payment",
    ]) == 0
    searched = _json_output(capsys)
    assert searched["results"][0]["id"] == memory_id

    assert main(["--db", str(db), "get", memory_id]) == 0
    assert _json_output(capsys)["id"] == memory_id

    backup = tmp_path / "backup.db"
    assert main(["--db", str(db), "backup", str(backup)]) == 0
    assert Path(_json_output(capsys)["backup"]).exists()

    assert main(["--db", str(db), "delete", memory_id]) == 0
    assert _json_output(capsys)["deleted"] is True

    assert main(["--db", str(db), "get", memory_id]) == 1
    assert _json_output(capsys)["error"] == "not_found"


def test_cli_strict_and_restore(tmp_path: Path, capsys) -> None:
    path = str(tmp_path / "memory.db")
    assert main(["--db", path, "put", "--content", "cli-scope-marker",
                 "--memory-type", "note", "--project", "analytics"]) == 0
    memory_id = _json_output(capsys)["id"]
    assert main(["--db", path, "search", "cli-scope-marker",
                 "--project", "payment", "--strict"]) == 0
    assert _json_output(capsys)["results"] == []
    assert main(["--db", path, "search", "cli-scope-marker",
                 "--project", "payment"]) == 0
    assert _json_output(capsys)["results"][0]["id"] == memory_id
    assert main(["--db", path, "delete", memory_id]) == 0
    assert _json_output(capsys)["deleted"]
    assert main(["--db", path, "restore", memory_id]) == 0
    assert _json_output(capsys)["restored"]
    assert main(["--db", path, "get", memory_id]) == 0
    assert _json_output(capsys)["id"] == memory_id
