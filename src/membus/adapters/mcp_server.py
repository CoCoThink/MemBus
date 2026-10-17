"""MCP adapter for MemBus."""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..db import Database
from ..models import MemoryCreate, SearchContext, SearchManyRequest, SearchRequest
from ..services.memory_service import MemoryService


def build_server(db_path: str | Path | None = None):
    """Build an MCPServer bound to a MemBus database."""

    try:
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "The MCP adapter requires the optional dependency: "
            "pip install 'membus[mcp]'"
        ) from exc

    path = Path(
        db_path
        or os.environ.get("MEMBUS_DB")
        or (Path.home() / ".membus" / "memory.db")
    ).expanduser()

    service = MemoryService(Database(path))
    mcp = MCPServer("MemBus")

    @mcp.tool()
    def memory_put(
        content: str,
        memory_type: str,
        scope: str,
        title: str = "",
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        source_type: str | None = None,
        source_ref: str | None = None,
    ) -> dict[str, Any]:
        """Store an explicit durable memory."""

        memory = service.put(
            MemoryCreate(
                title=title,
                content=content,
                memory_type=memory_type,
                scope=scope,
                workspace=workspace,
                project=project,
                repo=repo,
                branch=branch,
                source_type=source_type,
                source_ref=source_ref,
            )
        )
        return asdict(memory)

    @mcp.tool()
    def memory_get(
        memory_id: str,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        """Get one memory by stable ID."""

        memory = service.get(memory_id, include_deleted=include_deleted)
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
        """Logically delete one memory."""

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
        explain: bool = False,
    ) -> dict[str, Any]:
        """Search memories using exact/FTS retrieval and scoped ranking."""

        results = service.search(
            SearchRequest.with_types(
                query=query,
                context=SearchContext(
                    workspace=workspace,
                    project=project,
                    repo=repo,
                    branch=branch,
                ),
                types=types or (),
                limit=limit,
                explain=explain,
            )
        )
        return {
            "query": query,
            "results": [
                {
                    "memory": asdict(result.memory),
                    "score": result.score,
                    "signals": result.signals,
                }
                for result in results
            ],
        }

    @mcp.tool()
    def memory_search_many(
        queries: list[str],
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        types: list[str] | None = None,
        limit: int = 10,
        explain: bool = False,
    ) -> dict[str, Any]:
        """Search several lexical formulations in one round trip."""

        results = service.search_many(
            SearchManyRequest.from_queries(
                queries,
                context=SearchContext(
                    workspace=workspace,
                    project=project,
                    repo=repo,
                    branch=branch,
                ),
                types=types or (),
                limit=limit,
                explain=explain,
            )
        )
        return {
            "queries": queries,
            "results": [
                {
                    "memory": asdict(result.memory),
                    "score": result.score,
                    "signals": result.signals,
                }
                for result in results
            ],
        }

    return mcp


def main() -> None:
    """Run MemBus as a local stdio MCP server."""

    build_server().run()


if __name__ == "__main__":
    main()
