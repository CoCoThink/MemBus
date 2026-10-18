from __future__ import annotations

import json
from pathlib import Path

from membus.cli import main


def run(capsys, db: Path, *argv: str) -> tuple[int, dict]:
    code = main(["--db", str(db), *argv])
    return code, json.loads(capsys.readouterr().out)


def test_cli_put_search_get_delete_and_backup(tmp_path: Path, capsys) -> None:
    db = tmp_path / "memory.db"

    code, created = run(
        capsys, db, "put",
        "--content", "BUG-9001 is caused by payment retry logic.",
        "--title", "BUG-9001", "--memory-type", "incident",
        "--source", "cli-agent", "--project", "payment",
    )
    assert code == 0 and created["created"] is True
    memory = created["memory"]
    assert memory["source"] == "cli-agent" and memory["state"] == "active"

    code, searched = run(capsys, db, "search", "BUG-9001", "--project", "payment")
    assert code == 0 and searched["results"][0]["id"] == memory["id"]

    code, got = run(capsys, db, "get", memory["id"])
    assert code == 0 and got["id"] == memory["id"]

    backup = tmp_path / "backup.db"
    code, report = run(capsys, db, "backup", str(backup))
    assert code == 0 and Path(report["backup"]).exists()

    code, deleted = run(capsys, db, "delete", memory["id"],
                        "--expected-revision", str(memory["revision"]))
    assert code == 0 and deleted["deleted"] is True

    code, gone = run(capsys, db, "get", memory["id"])
    assert code == 0 and gone["state"] == "deleted"
    code, missing = run(capsys, db, "get", "mem_missing")
    assert code == 1 and missing["error"] == "not_found"


def test_cli_strict_and_restore(tmp_path: Path, capsys) -> None:
    db = tmp_path / "memory.db"
    _, created = run(capsys, db, "put", "--content", "cli-scope-marker",
                     "--memory-type", "note", "--project", "analytics")
    memory_id = created["memory"]["id"]

    _, strict = run(capsys, db, "search", "cli-scope-marker",
                    "--project", "payment", "--strict")
    assert strict["results"] == []
    _, broad = run(capsys, db, "search", "cli-scope-marker", "--project", "payment")
    assert broad["results"][0]["id"] == memory_id

    assert run(capsys, db, "delete", memory_id, "--expected-revision",
               str(created["memory"]["revision"]))[1]["deleted"]
    deleted = run(capsys, db, "get", memory_id)[1]
    assert run(capsys, db, "restore", memory_id, "--expected-revision",
               str(deleted["revision"]))[1]["restored"]
    assert run(capsys, db, "get", memory_id)[1]["state"] == "active"


def test_cli_update_requires_revision_and_reports_conflict(tmp_path: Path, capsys) -> None:
    db = tmp_path / "memory.db"
    _, created = run(capsys, db, "put", "--content", "versioned", "--memory-type",
                     "note", "--project", "payment", "--repo", "api")
    memory_id = created["memory"]["id"]

    code, updated = run(capsys, db, "update", memory_id, "--expected-revision", "1",
                        "--title", "Edited", "--repo", "")
    assert code == 0
    assert (updated["revision"], updated["title"], updated["repo"]) == (2, "Edited", None)

    code, conflict = run(capsys, db, "update", memory_id, "--expected-revision", "1",
                         "--title", "Stale")
    assert code == 1 and conflict["error"] == "revision_conflict"
    assert conflict["current"]["revision"] == 2

    code, invalid = run(capsys, db, "update", memory_id, "--expected-revision", "2")
    assert code == 2 and invalid["error"] == "invalid"


def test_cli_supersede_list_contexts_and_purge(tmp_path: Path, capsys) -> None:
    db = tmp_path / "memory.db"
    _, first = run(capsys, db, "put", "--content", "Settlement runs concurrently.",
                   "--memory-type", "decision", "--project", "payment")
    old_id = first["memory"]["id"]
    _, second = run(capsys, db, "put", "--content", "Settlement runs serially.",
                    "--memory-type", "decision", "--project", "payment",
                    "--supersedes", f"{old_id}:{first['memory']['revision']}")
    new_id = second["memory"]["id"]
    assert second["superseded"] == [old_id]

    _, listed = run(capsys, db, "list", "--project", "payment")
    assert [m["id"] for m in listed["memories"]] == [new_id]
    _, everything = run(capsys, db, "list", "--project", "payment", "--include-inactive")
    assert {m["state"] for m in everything["memories"]} == {"active", "superseded"}
    _, paged = run(capsys, db, "list", "--project", "payment",
                   "--include-inactive", "--limit", "1")
    assert paged["next_cursor"] is not None

    _, contexts = run(capsys, db, "contexts")
    assert contexts["contexts"] == [
        {"workspace": None, "project": "payment", "repo": None, "count": 1}
    ]

    code, purged = run(capsys, db, "purge", new_id, "--yes")
    assert code == 0 and purged["purged"] is True
    assert run(capsys, db, "get", old_id)[1]["state"] == "superseded"
    assert run(capsys, db, "get", new_id)[0] == 1


def test_cli_purge_requires_confirmation(tmp_path: Path, capsys) -> None:
    import pytest
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "m.db"), "purge", "mem_x"])
    capsys.readouterr()


def test_cli_sync_paging_and_purge_reset(tmp_path: Path, capsys) -> None:
    db = tmp_path / "memory.db"
    for content in ("first", "second"):
        assert run(capsys, db, "put", "--content", content, "--memory-type", "note")[0] == 0
    code, first = run(capsys, db, "sync", "--limit", "1")
    assert code == 0 and first["has_more"]
    assert first["memories"][0]["content"] == "first"
    code, second = run(capsys, db, "sync", "--after-revision", str(first["next_revision"]),
                       "--generation", first["generation"])
    assert code == 0 and not second["has_more"]
    assert second["memories"][0]["content"] == "second"
    code, invalid = run(capsys, db, "sync", "--after-revision", "1")
    assert code == 2 and invalid["error"] == "invalid"
    assert run(capsys, db, "purge", first["memories"][0]["id"], "--yes")[0] == 0
    code, reset = run(capsys, db, "sync", "--generation", first["generation"])
    assert code == 1 and reset["error"] == "sync_reset_required"
