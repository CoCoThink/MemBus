"""MCP v2 adapter for MemBus.

The adapter depends on the optional `mcp` extra. The MemBus core itself does
not import MCP.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..db import Database
from ..models import MemoryCreate, SearchContext, SearchManyRequest, SearchRequest
from ..services.memory_service import MemoryService


def build_server(
    db_path: str | Path | None = None,
    *,
    retrieval_log_mode: str | None = None,
):
    """Build an MCPServer bound to a MemBus database."""

    try:
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "The MCP adapter requires the optional dependency: "
            "pip install 'membus[mcp]'"
        ) from exc

    path = Path(
        db_path
        or os.environ.get("MEMBUS_DB")
        or (Path.home() / ".membus" / "memory.db")
    ).expanduser()

    log_mode = (
        retrieval_log_mode
        or os.environ.get("MEMBUS_RETRIEVAL_LOG_MODE")
        or "off"
    )

    service = MemoryService(Database(path), retrieval_log_mode=log_mode)
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
        importance: float = 0.5,
        confidence: float = 1.0,
        trust: float = 0.5,
        source_type: str | None = None,
        source_ref: str | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
        supersedes_id: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Store an explicit durable memory in the user-owned MemBus store."""

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
                importance=importance,
                confidence=confidence,
                trust=trust,
                source_type=source_type,
                source_ref=source_ref,
                valid_from=valid_from,
                valid_until=valid_until,
                supersedes_id=supersedes_id,
            ),
            actor=actor,
        )
        return asdict(memory)

    @mcp.tool()
    def memory_get(
        memory_id: str,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        """Get one memory by its stable ID."""

        memory = service.get(memory_id, include_deleted=include_deleted)
        return asdict(memory) if memory is not None else None

    @mcp.tool()
    def memory_update(
        memory_id: str,
        changes: dict[str, Any],
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Update mutable fields of one memory.

        Use explicit keys in `changes`; a key with value null clears an optional
        field, while an omitted key is left unchanged.
        """

        memory = service.update(memory_id, actor=actor, **changes)
        return asdict(memory)

    @mcp.tool()
    def memory_delete(
        memory_id: str,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Logically delete a memory while preserving its audit history."""

        return {
            "memory_id": memory_id,
            "deleted": service.delete(memory_id, actor=actor),
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
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        """Search memories using exact/FTS lexical retrieval and scoped ranking."""

        request = SearchRequest.with_types(
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
            include_deleted=include_deleted,
        )
        results = service.search(request)
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
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        """Search several lexical formulations in one adapter round trip."""

        request = SearchManyRequest.from_queries(
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
            include_deleted=include_deleted,
        )
        results = service.search_many(request)
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
