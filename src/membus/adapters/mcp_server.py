"""MCP adapter for MemBus."""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..models import MemoryCreate, SearchContext, SearchRequest
from ..services.memory_service import MemoryService
from ..store import SQLiteStore


def build_server(db_path: str | Path | None = None):
    try:
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The MCP adapter requires: pip install 'membus[mcp]'"
        ) from exc

    path = Path(
        db_path
        or os.environ.get("MEMBUS_DB")
        or (Path.home() / ".membus" / "memory.db")
    ).expanduser()

    service = MemoryService(SQLiteStore(path))
    mcp = MCPServer("MemBus")

    @mcp.tool()
    def memory_put(
        content: str,
        memory_type: str,
        title: str = "",
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        source_type: str | None = None,
        source_ref: str | None = None,
    ) -> dict[str, Any]:
        """Store an explicit durable memory."""

        return asdict(
            service.put(
                MemoryCreate(
                    title=title,
                    content=content,
                    memory_type=memory_type,
                    workspace=workspace,
                    project=project,
                    repo=repo,
                    branch=branch,
                    source_type=source_type,
                    source_ref=source_ref,
                )
            )
        )

    @mcp.tool()
    def memory_get(memory_id: str) -> dict[str, Any] | None:
        """Get one memory by stable ID."""

        memory = service.get(memory_id)
        return asdict(memory) if memory is not None else None

    @mcp.tool()
    def memory_update(
        memory_id: str,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        """Update mutable fields of one memory."""

        return asdict(service.update(memory_id, **changes))

    @mcp.tool()
    def memory_delete(memory_id: str) -> dict[str, Any]:
        """Delete one memory."""

        return {
            "memory_id": memory_id,
            "deleted": service.delete(memory_id),
        }

    @mcp.tool()
    def memory_search(
        query: str,
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        types: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search memories with exact matching, aliases, FTS5, and context isolation."""

        results = service.search(
            SearchRequest(
                query=query,
                context=SearchContext(
                    workspace=workspace,
                    project=project,
                    repo=repo,
                    branch=branch,
                ),
                types=tuple(types or ()),
                limit=limit,
            )
        )
        return {
            "query": query,
            "results": [
                {
                    "memory": asdict(result.memory),
                    "match": result.match,
                }
                for result in results
            ],
        }

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
