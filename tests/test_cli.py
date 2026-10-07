from __future__ import annotations

import json
from pathlib import Path

from membus.cli import main


ROOT = Path(__file__).resolve().parents[1]


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
        "--scope", "project",
        "--project", "payment",
    ]) == 0
    created = _json_output(capsys)
    memory_id = created["id"]

    assert main([
        "--db", str(db),
        "search",
        "BUG-9001",
        "--project", "payment",
        "--explain",
    ]) == 0
    searched = _json_output(capsys)
    assert searched["results"][0]["memory"]["id"] == memory_id
    assert searched["results"][0]["signals"]["exact_match"] is True

    assert main(["--db", str(db), "get", memory_id]) == 0
    loaded = _json_output(capsys)
    assert loaded["id"] == memory_id

    backup = tmp_path / "backup.db"
    assert main(["--db", str(db), "backup", str(backup)]) == 0
    backed_up = _json_output(capsys)
    assert Path(backed_up["backup"]).exists()

    assert main(["--db", str(db), "delete", memory_id]) == 0
    deleted = _json_output(capsys)
    assert deleted["deleted"] is True

    assert main(["--db", str(db), "get", memory_id]) == 1
    missing = _json_output(capsys)
    assert missing["error"] == "not_found"


def test_cli_eval_gate(capsys) -> None:
    dataset = ROOT / "evals" / "golden_v1.json"

    assert main([
        "eval",
        str(dataset),
        "--min-recall-5", "0.95",
        "--min-recall-10", "0.95",
        "--max-wrong-scope-rate", "0.0",
    ]) == 0

    report = _json_output(capsys)
    assert report["recall_at_5"] >= 0.95
    assert report["wrong_scope_hit_rate"] == 0.0
