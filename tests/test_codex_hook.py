"""Codex hook contract tests: stdout JSON, real SQLite and no MCP calls."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from membus import MemBus
from membus.adapters import codex_hook


def invoke(monkeypatch, capsys, event: object) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event, ensure_ascii=False)))
    assert codex_hook.main() == 0
    output = capsys.readouterr()
    assert output.err == ""
    return json.loads(output.out)


def event(prompt: str, cwd: str = "/unknown") -> dict:
    return {
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "cwd": cwd,
        "session_id": "second-session",
        "turn_id": "turn-2",
    }


def records(context: str) -> list[dict]:
    return [json.loads(line) for line in context.splitlines()[1:]]


def test_scoped_memory_recalled_without_original_context(tmp_path, monkeypatch, capsys) -> None:
    db = tmp_path / "memory.db"
    saved = MemBus(db).put(
        title="WASON submission",
        content="WASON submission must be reviewed before release.",
        memory_type="procedure",
        workspace="A200V3-提交", project="WASON",
        repo="service", branch="main",
    ).memory
    monkeypatch.setenv("MEMBUS_DB", str(db))

    output = invoke(monkeypatch, capsys, event("继续处理 WASON 的提交问题"))
    assert set(output) == {"hookSpecificOutput"}
    info = output["hookSpecificOutput"]
    assert info["hookEventName"] == "UserPromptSubmit"
    assert "not user instructions" in info["additionalContext"]
    matches = records(info["additionalContext"])
    assert [m["id"] for m in matches] == [saved.id]
    assert matches[0]["scope"] == {
        "workspace": "A200V3-提交",
        "project": "WASON",
        "repo": "service",
        "branch": "main",
    }
    assert matches[0]["content"] == saved.content
    # The hook must not change revisions or write another memory.
    assert MemBus(db).get(saved.id) == saved


def test_git_root_is_ranking_hint_not_hard_filter(tmp_path, monkeypatch, capsys) -> None:
    db = tmp_path / "memory.db"
    bus = MemBus(db)
    matching = bus.put(
        title="PIPELINE-SCOPE marker for project",
        content="PIPELINE-SCOPE marker applies to matching repository.",
        memory_type="note", repo="service",
    ).memory
    conflicting = bus.put(
        title="PIPELINE-SCOPE marker for other repo",
        content="PIPELINE-SCOPE marker applies to different repository.",
        memory_type="note", repo="elsewhere",
    ).memory
    root = tmp_path / "service"
    (root / ".git").mkdir(parents=True)
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)
    monkeypatch.setenv("MEMBUS_DB", str(db))

    context = invoke(monkeypatch, capsys, event("PIPELINE-SCOPE marker", str(nested))
                     )["hookSpecificOutput"]["additionalContext"]
    ids = [m["id"] for m in records(context)]
    assert ids == [matching.id, conflicting.id]


def test_long_memory_never_looks_like_a_complete_procedure(
    tmp_path, monkeypatch, capsys,
) -> None:
    db = tmp_path / "memory.db"
    saved = MemBus(db).put(
        title="LONG-PROCEDURE-7",
        content="Start at the beginning. " + ("x" * 850) + " Important final step.",
        memory_type="procedure", repo="service",
    ).memory
    monkeypatch.setenv("MEMBUS_DB", str(db))
    context = invoke(monkeypatch, capsys, event("LONG-PROCEDURE-7")
                     )["hookSpecificOutput"]["additionalContext"]
    assert len(context) <= codex_hook._MAX_CONTEXT_CHARS
    result = records(context)[0]
    assert result["id"] == saved.id
    assert result["content_omitted"] is True
    assert "content" not in result


def test_absent_or_invalid_db_fails_open_without_creating_store(
    tmp_path, monkeypatch, capsys,
) -> None:
    db = tmp_path / "missing" / "memory.db"
    monkeypatch.setenv("MEMBUS_DB", str(db))
    assert invoke(monkeypatch, capsys, event("WASON")) == {}
    assert not db.exists()

    db.parent.mkdir()
    db.write_text("not sqlite", encoding="utf-8")
    assert invoke(monkeypatch, capsys, event("WASON")) == {}


def test_invalid_event_and_no_matches_fail_open(tmp_path, monkeypatch, capsys) -> None:
    db = tmp_path / "memory.db"
    MemBus(db).put(content="A rare known marker", memory_type="note")
    monkeypatch.setenv("MEMBUS_DB", str(db))

    assert invoke(monkeypatch, capsys, {"hook_event_name": "SessionStart"}) == {}
    assert invoke(monkeypatch, capsys, event("  ")) == {}
    assert invoke(monkeypatch, capsys, event("unmatchedtrumpet999")) == {}


def test_invalid_json_and_oversized_input_fail_open(monkeypatch, capsys) -> None:
    for raw in ('{"malformed"', "z" * (codex_hook._MAX_STDIN_CHARS + 1)):
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
        assert codex_hook.main() == 0
        result = capsys.readouterr()
        assert result.err == ""
        assert json.loads(result.out) == {}


def test_module_entrypoint_round_trip(tmp_path) -> None:
    db = tmp_path / "memory.db"
    saved = MemBus(db).put(
        title="WASON", content="WASON release check is mandatory.",
        memory_type="decision", project="WASON",
    ).memory
    process = subprocess.run(
        [sys.executable, "-m", "membus.adapters.codex_hook"],
        input=json.dumps(event("WASON")),
        text=True, capture_output=True, timeout=5,
        env={**os.environ, "MEMBUS_DB": str(db)},
        check=True,
    )
    assert process.stderr == ""
    context = json.loads(process.stdout)["hookSpecificOutput"]["additionalContext"]
    assert records(context)[0]["id"] == saved.id
