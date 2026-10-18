from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import Implementation

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
            "memory_put", "memory_update", "memory_delete", "memory_restore",
        }
        for name, tool in tools.items():
            assert tool.description == DESCRIPTIONS[name]
        # ctx is injected by the server, never part of the agent-facing schema.
        assert "ctx" not in tools["memory_put"].input_schema["properties"]
        update_props = tools["memory_update"].input_schema["properties"]
        assert "expected_version" in update_props
        assert "changes" not in update_props
        assert "expected_version" in tools["memory_update"].input_schema["required"]
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
            "source": "claude-code",
        })
        put = created.structured_content
        memory = put["memory"]
        assert put["created"] is True and put["similar"] == []
        assert memory["source"] == "claude-code"
        assert memory["state"] == "active" and memory["version"] == 1
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

        deleted = await client.call_tool("memory_delete", {"memory_id": memory_id})
        assert deleted.structured_content["deleted"]
        gone = await client.call_tool("memory_get", {"memory_id": memory_id})
        assert gone.structured_content["state"] == "deleted"
        restored = await client.call_tool("memory_restore", {"memory_id": memory_id})
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
            "supersedes": [old["id"]],
        })
        assert replaced.structured_content["superseded"] == [old["id"]]
        new = replaced.structured_content["memory"]

        stale = (await client.call_tool("memory_get", {"memory_id": old["id"]})
                 ).structured_content
        assert stale["state"] == "superseded"
        assert stale["superseded_by"] == new["id"]
        inactive = await client.call_tool("memory_list", {
            "project": "payment", "include_inactive": True})
        assert {m["state"] for m in inactive.structured_content["memories"]} == {
            "active", "superseded"}

        updated = await client.call_tool("memory_update", {
            "memory_id": new["id"], "expected_version": 1,
            "title": "Serial settlement", "repo": "payment-api"})
        assert updated.structured_content["version"] == 2
        assert updated.structured_content["repo"] == "payment-api"

        # Empty string clears; omitted fields are unchanged.
        cleared = await client.call_tool("memory_update", {
            "memory_id": new["id"], "expected_version": 2, "repo": ""})
        assert cleared.structured_content["repo"] is None
        assert cleared.structured_content["title"] == "Serial settlement"

        conflict = await client.call_tool("memory_update", {
            "memory_id": new["id"], "expected_version": 1, "title": "stale"})
        payload = error_payload(conflict)
        assert payload["error"] == "version_conflict"
        assert payload["current"]["version"] == 3

        missing = await client.call_tool("memory_update", {
            "memory_id": "mem_missing", "expected_version": 1, "title": "x"})
        assert error_payload(missing) == {"error": "not_found", "id": "mem_missing"}

        invalid = await client.call_tool("memory_put", {
            "content": "x", "memory_type": "nonsense"})
        assert error_payload(invalid)["error"] == "invalid"

        dup_a = (await client.call_tool("memory_put", {
            "content": "Alpha", "memory_type": "note"})).structured_content["memory"]
        await client.call_tool("memory_put", {"content": "Beta", "memory_type": "note"})
        dup = await client.call_tool("memory_update", {
            "memory_id": dup_a["id"], "expected_version": 1, "content": "Beta"})
        assert error_payload(dup)["error"] == "duplicate_memory"


@pytest.mark.anyio
async def test_mcp_source_defaults_to_client_name(tmp_path: Path) -> None:
    server = build_server(tmp_path / "memory.db")
    client_info = Implementation(name="codex-cli", version="1.0")
    async with Client(server, client_info=client_info) as client:
        implicit = await client.call_tool("memory_put", {
            "content": "written without source", "memory_type": "note"})
        explicit = await client.call_tool("memory_put", {
            "content": "written with source", "memory_type": "note",
            "source": "manual"})
    assert implicit.structured_content["memory"]["source"] == "codex-cli"
    assert explicit.structured_content["memory"]["source"] == "manual"


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
