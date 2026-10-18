from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from membus.adapters.mcp_server import build_server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_tool_surface_and_round_trip(tmp_path: Path) -> None:
    server = build_server(tmp_path / "memory.db")

    async with Client(server, raise_exceptions=True) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}

        assert names == {
            "memory_put",
            "memory_get",
            "memory_update",
            "memory_delete",
            "memory_restore",
            "memory_search",
        }

        created = await client.call_tool(
            "memory_put",
            {
                "title": "Generated client",
                "content": "generated/client.ts comes from OpenAPI.",
                "memory_type": "procedure",
                "project": "payment",
                "repo": "payment-api",
                "source": "claude-code",
            },
        )
        memory = created.structured_content
        assert memory is not None
        memory_id = memory["id"]
        assert memory["source"] == "claude-code"
        assert memory["created_at"] == memory["updated_at"]

        searched = await client.call_tool(
            "memory_search",
            {
                "query": "generated/client.ts",
                "project": "payment",
                "repo": "payment-api",
            },
        )
        payload = searched.structured_content
        assert payload is not None
        assert payload["results"][0]["id"] == memory_id

        foreign = await client.call_tool(
            "memory_put",
            {"content": "generated/client.ts unrelated",
             "memory_type": "note", "project": "analytics"},
        )
        foreign_id = foreign.structured_content["id"]
        strict_results = await client.call_tool(
            "memory_search",
            {"query": "generated/client.ts", "project": "payment",
             "repo": "payment-api"},
        )
        assert foreign_id not in {
            m["id"] for m in strict_results.structured_content["results"]
        }
        broad = await client.call_tool(
            "memory_search",
            {"query": "generated/client.ts", "project": "payment",
             "strict": False},
        )
        assert foreign_id in {m["id"] for m in broad.structured_content["results"]}
        deleted = await client.call_tool("memory_delete", {"memory_id": memory_id})
        assert deleted.structured_content["deleted"]
        restored = await client.call_tool("memory_restore", {"memory_id": memory_id})
        assert restored.structured_content["restored"]
        recovered = await client.call_tool(
            "memory_search",
            {"query": "generated/client.ts", "project": "payment",
             "repo": "payment-api"},
        )
        assert memory_id in {
            m["id"] for m in recovered.structured_content["results"]
        }
