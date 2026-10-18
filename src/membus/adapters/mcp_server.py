"""MCP adapter for MemBus."""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..core import MemBus


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

    bus = MemBus(path)
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
        source: str | None = None,
    ) -> dict[str, Any]:
        """Store a durable memory."""

        return asdict(
            bus.put(
                title=title,
                content=content,
                memory_type=memory_type,
                workspace=workspace,
                project=project,
                repo=repo,
                branch=branch,
                source=source,
            )
        )

    @mcp.tool()
    def memory_get(memory_id: str) -> dict[str, Any] | None:
        """Get one memory by ID."""

        memory = bus.get(memory_id)
        return asdict(memory) if memory is not None else None

    @mcp.tool()
    def memory_update(
        memory_id: str,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        """Update mutable fields of one memory."""

        return asdict(bus.update(memory_id, **changes))

    @mcp.tool()
    def memory_delete(memory_id: str) -> dict[str, Any]:
        """Soft-delete one memory, recoverable via memory_restore."""

        return {
            "memory_id": memory_id,
            "deleted": bus.delete(memory_id),
        }

    @mcp.tool()
    def memory_restore(memory_id: str) -> dict[str, Any]:
        """Restore a soft-deleted memory."""
        return {"memory_id": memory_id, "restored": bus.restore(memory_id)}

    @mcp.tool()
    def memory_search(
        query: str,
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        types: list[str] | None = None,
        limit: int = 10,
        strict: bool = True,
    ) -> dict[str, Any]:
        """Search memories with exact matching, aliases, FTS5, and context isolation."""

        results = bus.search(
            query,
            workspace=workspace,
            project=project,
            repo=repo,
            branch=branch,
            types=types or (),
            limit=limit,
            strict=strict,
        )
        return {
            "query": query,
            "results": [asdict(memory) for memory in results],
        }

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
