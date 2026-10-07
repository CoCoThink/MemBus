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
            "memory_search",
            "memory_search_many",
        }

        created = await client.call_tool(
            "memory_put",
            {
                "title": "Generated client",
                "content": "generated/client.ts comes from OpenAPI.",
                "memory_type": "procedure",
                "scope": "repo",
                "project": "payment",
                "repo": "payment-api",
                "trust": 1.0,
            },
        )
        memory = created.structured_content
        assert memory is not None
        memory_id = memory["id"]

        searched = await client.call_tool(
            "memory_search",
            {
                "query": "generated/client.ts",
                "project": "payment",
                "repo": "payment-api",
                "explain": True,
            },
        )
        payload = searched.structured_content
        assert payload is not None
        assert payload["results"][0]["memory"]["id"] == memory_id
        assert payload["results"][0]["signals"]["exact_match"] is True
