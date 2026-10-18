from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import Implementation

from membus import MemBus
from membus.adapters.mcp_server import DESCRIPTIONS, INSTRUCTIONS, build_server

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def ids(payload: dict, key: str = "results") -> list[str]:
    return [m["id"] for m in payload[key]]


def error_payload(result) -> dict:
    assert result.is_error
    text = result.content[0].text
    return json.loads(text[text.index("{"):])


@pytest.mark.anyio
async def test_mcp_tool_surface(tmp_path: Path) -> None:
    server = build_server(tmp_path / "memory.db")
    async with Client(server, raise_exceptions=False) as client:
        listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}

        assert set(tools) == set(DESCRIPTIONS) == {
            "memory_search", "memory_list", "memory_contexts", "memory_get",
            "memory_put", "memory_update", "memory_delete", "memory_restore", "memory_sync",
        }
        for name, tool in tools.items():
            assert tool.description == DESCRIPTIONS[name]
        # ctx is injected by the server, never part of the agent-facing schema.
        assert "ctx" not in tools["memory_put"].input_schema["properties"]
        update_props = tools["memory_update"].input_schema["properties"]
        assert "expected_revision" in update_props
        assert "changes" not in update_props
        assert "expected_revision" in tools["memory_update"].input_schema["required"]
        for name in ("memory_delete", "memory_restore"):
            assert "expected_revision" in tools[name].input_schema["required"]
        assert "updated_since" in tools["memory_list"].input_schema["properties"]
        # Only programs with persistent state can sync; keep it out of every
        # agent's instructions.
        assert "memory_sync" not in INSTRUCTIONS
        assert client.instructions == INSTRUCTIONS


@pytest.mark.anyio
async def test_mcp_round_trip(tmp_path: Path) -> None:
    server = build_server(tmp_path / "memory.db")
    async with Client(server, raise_exceptions=False) as client:
        created = await client.call_tool("memory_put", {
            "title": "Generated client",
            "content": "generated/client.ts comes from OpenAPI.",
            "memory_type": "procedure",
            "project": "payment", "repo": "payment-api",
        })
        put = created.structured_content
        memory = put["memory"]
        assert put["created"] is True and put["similar"] == []
        assert memory["state"] == "active" and memory["revision"] == 1
        memory_id = memory["id"]

        again = await client.call_tool("memory_put", {
            "title": "Generated client",
            "content": "generated/client.ts comes from OpenAPI.",
            "memory_type": "procedure",
            "project": "payment", "repo": "payment-api",
        })
        assert again.structured_content["created"] is False
        assert again.structured_content["memory"]["id"] == memory_id

        found = await client.call_tool("memory_search", {
            "query": "generated/client.ts", "project": "payment",
            "repo": "payment-api",
        })
        assert ids(found.structured_content) == [memory_id]

        listed = await client.call_tool("memory_list", {
            "project": "payment", "repo": "payment-api",
            "types": ["procedure"],
        })
        assert ids(listed.structured_content, "memories") == [memory_id]
        assert listed.structured_content["next_cursor"] is None

        contexts = await client.call_tool("memory_contexts", {})
        assert contexts.structured_content["contexts"] == [
            {"workspace": None, "project": "payment", "repo": "payment-api",
             "count": 1}
        ]

        fetched = await client.call_tool("memory_get", {"memory_id": memory_id})
        assert fetched.structured_content["id"] == memory_id

        deleted = await client.call_tool("memory_delete", {
            "memory_id": memory_id, "expected_revision": memory["revision"]})
        assert deleted.structured_content["deleted"]
        gone = await client.call_tool("memory_get", {"memory_id": memory_id})
        assert gone.structured_content["state"] == "deleted"
        restored = await client.call_tool("memory_restore", {
            "memory_id": memory_id, "expected_revision": gone.structured_content["revision"]})
        assert restored.structured_content["restored"]
        missing = await client.call_tool("memory_get", {"memory_id": "mem_nope"})
        assert error_payload(missing) == {"error": "not_found", "id": "mem_nope"}
        recovered = await client.call_tool("memory_search", {
            "query": "generated/client.ts", "project": "payment",
        })
        assert memory_id in ids(recovered.structured_content)


@pytest.mark.anyio
async def test_mcp_strict_and_broad_search(tmp_path: Path) -> None:
    server = build_server(tmp_path / "memory.db")
    async with Client(server, raise_exceptions=False) as client:
        own = (await client.call_tool("memory_put", {
            "content": "generated/client.ts is generated", "memory_type": "note",
            "project": "payment", "repo": "payment-api"})).structured_content["memory"]["id"]
        foreign = (await client.call_tool("memory_put", {
            "content": "generated/client.ts unrelated", "memory_type": "note",
            "project": "analytics"})).structured_content["memory"]["id"]

        strict = await client.call_tool("memory_search", {
            "query": "generated/client.ts", "project": "payment",
            "repo": "payment-api", "strict": True})
        assert ids(strict.structured_content) == [own]
        broad = await client.call_tool("memory_search", {
            "query": "generated/client.ts", "project": "payment"})
        explicit = await client.call_tool("memory_search", {
            "query": "generated/client.ts", "project": "payment", "strict": False})
        assert foreign in ids(broad.structured_content)
        assert ids(broad.structured_content) == ids(explicit.structured_content)


@pytest.mark.anyio
async def test_mcp_supersede_update_and_errors(tmp_path: Path) -> None:
    server = build_server(tmp_path / "memory.db")
    async with Client(server, raise_exceptions=False) as client:
        old = (await client.call_tool("memory_put", {
            "content": "Settlement runs concurrently.",
            "memory_type": "decision", "project": "payment",
        })).structured_content["memory"]

        replaced = await client.call_tool("memory_put", {
            "content": "Settlement must run serially.",
            "memory_type": "decision", "project": "payment",
            "supersedes": [{"id": old["id"], "expected_revision": old["revision"]}],
        })
        assert replaced.structured_content["superseded"] == [old["id"]]
        new = replaced.structured_content["memory"]
        retry = await client.call_tool("memory_put", {
            "content": "Settlement must run serially.",
            "memory_type": "decision", "project": "payment",
            "supersedes": [{"id": old["id"], "expected_revision": old["revision"]}],
        })
        assert not retry.is_error and not retry.structured_content["created"]
        assert retry.structured_content["memory"] == new

        stale = (await client.call_tool("memory_get", {"memory_id": old["id"]})
                 ).structured_content
        assert stale["state"] == "superseded"
        assert stale["superseded_by"] == new["id"]
        inactive = await client.call_tool("memory_list", {
            "project": "payment", "include_inactive": True})
        assert {m["state"] for m in inactive.structured_content["memories"]} == {
            "active", "superseded"}

        updated = await client.call_tool("memory_update", {
            "memory_id": new["id"], "expected_revision": new["revision"],
            "title": "Serial settlement", "repo": "payment-api"})
        assert updated.structured_content["revision"] > new["revision"]
        assert updated.structured_content["repo"] == "payment-api"

        # Empty string clears; omitted fields are unchanged.
        cleared = await client.call_tool("memory_update", {
            "memory_id": new["id"], "expected_revision": updated.structured_content["revision"],
            "repo": ""})
        assert cleared.structured_content["repo"] is None
        assert cleared.structured_content["title"] == "Serial settlement"

        conflict = await client.call_tool("memory_update", {
            "memory_id": new["id"], "expected_revision": 1, "title": "stale"})
        payload = error_payload(conflict)
        assert payload["error"] == "revision_conflict"
        assert payload["current"]["revision"] == cleared.structured_content["revision"]

        missing = await client.call_tool("memory_update", {
            "memory_id": "mem_missing", "expected_revision": 1, "title": "x"})
        assert error_payload(missing) == {"error": "not_found", "id": "mem_missing"}

        invalid = await client.call_tool("memory_put", {
            "content": "x", "memory_type": "nonsense"})
        assert error_payload(invalid)["error"] == "invalid"

        dup_a = (await client.call_tool("memory_put", {
            "content": "Alpha", "memory_type": "note"})).structured_content["memory"]
        await client.call_tool("memory_put", {"content": "Beta", "memory_type": "note"})
        dup = await client.call_tool("memory_update", {
            "memory_id": dup_a["id"], "expected_revision": dup_a["revision"], "content": "Beta"})
        assert error_payload(dup)["error"] == "duplicate_memory"


@pytest.mark.anyio
async def test_mcp_sync_inactive_states_and_generation_reset(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    async with Client(build_server(path), raise_exceptions=False) as client:
        created = (await client.call_tool("memory_put", {
            "content": "cached", "memory_type": "note", "project": "own",
        })).structured_content["memory"]
        page = (await client.call_tool("memory_sync", {})).structured_content
        assert page["memories"] == [created] and not page["has_more"]
        deleted = await client.call_tool("memory_delete", {
            "memory_id": created["id"], "expected_revision": created["revision"]})
        assert not deleted.is_error
        poll = (await client.call_tool("memory_sync", {
            "after_revision": page["next_revision"], "generation": page["generation"],
        })).structured_content
        assert poll["memories"][0]["state"] == "deleted"
        stale = await client.call_tool("memory_restore", {
            "memory_id": created["id"], "expected_revision": created["revision"]})
        assert error_payload(stale)["error"] == "revision_conflict"
        invalid = await client.call_tool("memory_sync", {"after_revision": page["next_revision"]})
        assert error_payload(invalid)["error"] == "invalid"
        MemBus(path).purge(created["id"])
        reset = await client.call_tool("memory_sync", {
            "after_revision": poll["next_revision"], "generation": poll["generation"],
        })
        assert error_payload(reset)["error"] == "sync_reset_required"


@pytest.mark.anyio
async def test_mcp_source_is_last_writing_client(tmp_path: Path) -> None:
    server = build_server(tmp_path / "memory.db")
    codex = Implementation(name="codex-cli", version="1.0")
    claude = Implementation(name="claude-code", version="1.0")
    async with Client(server, client_info=codex) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name in ("memory_put", "memory_update"):
            assert "source" not in tools[name].input_schema["properties"]
        put = await client.call_tool("memory_put", {
            "content": "written by codex", "memory_type": "note"})
    memory = put.structured_content["memory"]
    assert memory["source"] == "codex-cli"
    async with Client(server, client_info=claude) as client:
        updated = await client.call_tool("memory_update", {
            "memory_id": memory["id"], "expected_revision": memory["revision"],
            "content": "edited by claude"})
    assert updated.structured_content["source"] == "claude-code"


@pytest.mark.anyio
async def test_mcp_restore_returns_similar(tmp_path: Path) -> None:
    server = build_server(tmp_path / "memory.db")
    async with Client(server) as client:
        async def call(name, args):
            return (await client.call_tool(name, args)).structured_content

        old = (await call("memory_put", {
            "content": "Deploy with fabric", "memory_type": "procedure"}))["memory"]
        new = (await call("memory_put", {
            "content": "Deploy with ansible", "memory_type": "procedure",
            "supersedes": [{"id": old["id"], "expected_revision": old["revision"]}],
        }))["memory"]
        await call("memory_delete", {
            "memory_id": new["id"], "expected_revision": new["revision"]})
        stale = await call("memory_get", {"memory_id": old["id"]})
        await call("memory_restore", {
            "memory_id": old["id"], "expected_revision": stale["revision"]})

        # Restoring the deleted successor now leaves two live answers.
        deleted = await call("memory_get", {"memory_id": new["id"]})
        restored = await call("memory_restore", {
            "memory_id": new["id"], "expected_revision": deleted["revision"]})
        assert restored["restored"] is True
        assert [m["id"] for m in restored["similar"]] == [old["id"]]

        noop = await call("memory_restore", {
            "memory_id": new["id"],
            "expected_revision": (await call("memory_get", {"memory_id": new["id"]}))["revision"],
        })
        assert noop == {"memory_id": new["id"], "restored": False, "similar": []}


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert match, heading
    return match.group(1)


def test_agent_protocol_doc_matches_adapter() -> None:
    doc = (ROOT / "docs" / "agent-protocol.md").read_text(encoding="utf-8")

    block = re.search(r"```text\n(.*?)```", _section(doc, "Server instructions"), re.S)
    assert block is not None
    assert block.group(1) == INSTRUCTIONS

    documented = dict(re.findall(
        r"^\*\*(memory_\w+)\*\* — (.+)$", _section(doc, "Tool descriptions"), re.M,
    ))
    assert documented == DESCRIPTIONS
