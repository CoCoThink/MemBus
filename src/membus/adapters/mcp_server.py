"""MCP adapter for MemBus.

Tool descriptions and server instructions are the agent-facing protocol; the
normative text lives in docs/agent-protocol.md and is checked against this
module by tests.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from ..core import (
    UNSET,
    DuplicateMemory,
    MemBus,
    NotFound,
    VersionConflict,
)

try:  # the MCP SDK is an optional dependency
    from mcp.server import MCPServer
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover
    MCPServer = Context = ToolError = None  # type: ignore[assignment,misc]

INSTRUCTIONS = """\
MemBus is the user's shared long-term memory, used by all of their AI agents.
Treat stored memories as the user's own notes: trusted, but check that a
memory's context applies to the current task before acting on it.

Session start
  1. Call memory_contexts once to learn existing workspace/project/repo names.
  2. Call memory_list with the current context and
     types=[preference, procedure, decision] to load what applies here.

During work
  - Before answering from assumptions about the user, their projects, or past
    decisions, call memory_search.
  - Pass the current context on every search/list/put.

Writing
  - Write when you learn something durable that a future session or another
    agent would need: a preference, a decision and its reason, a procedure,
    an incident and its cause, a stable fact. Do not store transient task
    state, secrets, or things already obvious from the code.
  - One memory = one idea. Short title, self-contained content.
  - Search first. If an existing memory is the same idea:
      * it was worded wrong or scoped wrong  -> memory_update
      * the world changed (decision reversed, procedure replaced, fact no
        longer true)                         -> memory_put with supersedes=[id]
  - After memory_put, read `similar`. If one is the same idea, fix it with the
    rule above instead of keeping two.

Context naming
  - Reuse names returned by memory_contexts exactly.
  - workspace: broad domain (work, personal, health, ...).
  - project: product or initiative, lowercase-kebab.
  - repo: repository name without owner, lowercase (payment-api).
  - branch: only for memories that stop being true when the branch merges.
  - Scope as broadly as is true: a preference that holds everywhere has no
    context; a repo convention has workspace+project+repo.
"""

DESCRIPTIONS = {
    "memory_search": (
        "Ranked search over active memories by exact text, words, identifiers, "
        "or Chinese phrases. Pass the current workspace/project/repo/branch: "
        "matching context ranks first; with strict=false (default) memories "
        "from other contexts may follow as fallback, so check each result's "
        "context before relying on it. strict=true returns only memories that "
        "apply to the given context."
    ),
    "memory_list": (
        "List active memories that apply to the given context, newest first, "
        "without needing a query. Use at session start with "
        "types=[preference, procedure, decision]. updated_since=<timestamp> "
        "returns only changes since then; include_inactive=true also returns "
        "deleted and superseded memories (check `state`). Page with cursor."
    ),
    "memory_contexts": (
        "List workspace/project/repo combinations in use with memory counts. "
        "Call before writing so you reuse existing names exactly."
    ),
    "memory_get": (
        "Get one memory by ID in any state. If `superseded_by` is set, the "
        "memory is outdated; follow that ID."
    ),
    "memory_put": (
        "Store one durable memory. Identical content in the same context "
        "returns the existing memory (created=false). supersedes=[ids] marks "
        "older active memories as replaced because the world changed. Returns "
        "`similar` near-matches: if one is the same idea, update or supersede "
        "it instead of keeping both. memory_type: preference | procedure | "
        "decision | fact | incident | note."
    ),
    "memory_update": (
        "Correct an active memory's wording, type, or context (same idea, not "
        "a change in the world — use memory_put with supersedes for that). "
        "Requires expected_version from your last read; on version_conflict "
        "the error contains the current memory: re-apply your change to it "
        "and retry. Omitted fields are unchanged; an empty string clears "
        "title, a context field, or source."
    ),
    "memory_delete": (
        "Soft-delete a memory that is wrong or no longer useful (prefer "
        "supersedes when it was replaced by something newer). Recoverable "
        "with memory_restore. Deleting a memory that superseded others makes "
        "them active again."
    ),
    "memory_restore": "Undo memory_delete.",
}


@contextlib.contextmanager
def _tool_errors() -> Iterator[None]:
    """Turn domain errors into ToolError whose message carries JSON."""
    def fail(payload: dict[str, Any]) -> ToolError:
        return ToolError(json.dumps(payload, ensure_ascii=False))

    try:
        yield
    except NotFound as exc:
        raise fail({"error": "not_found", "id": exc.memory_id}) from None
    except VersionConflict as exc:
        raise fail({
            "error": "version_conflict",
            "hint": "re-apply your change to `current` and retry with its version",
            "current": exc.current.to_dict(),
        }) from None
    except DuplicateMemory as exc:
        raise fail({
            "error": "duplicate_memory",
            "hint": "an identical active memory exists; use or update it",
            "existing": exc.existing.to_dict(),
        }) from None
    except ValueError as exc:
        raise fail({"error": "invalid", "detail": str(exc)}) from None


def _client_name(ctx: Any) -> str | None:
    try:
        return ctx.session.client_params.client_info.name or None
    except AttributeError:
        return None


def _unset_if_none(value: Any) -> Any:
    return UNSET if value is None else value


def build_server(db_path: str | Path | None = None):
    if MCPServer is None:  # pragma: no cover
        raise RuntimeError("The MCP adapter requires: pip install 'membus[mcp]'")

    path = Path(
        db_path
        or os.environ.get("MEMBUS_DB")
        or (Path.home() / ".membus" / "memory.db")
    ).expanduser()

    bus = MemBus(path)
    mcp = MCPServer("MemBus", instructions=INSTRUCTIONS)

    @mcp.tool(description=DESCRIPTIONS["memory_search"])
    def memory_search(
        query: str,
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        types: list[str] | None = None,
        limit: int = 10,
        strict: bool = False,
    ) -> dict[str, Any]:
        with _tool_errors():
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
            "results": [memory.to_dict() for memory in results],
        }

    @mcp.tool(description=DESCRIPTIONS["memory_list"])
    def memory_list(
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        types: list[str] | None = None,
        updated_since: str | None = None,
        include_inactive: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        with _tool_errors():
            page = bus.list(
                workspace=workspace,
                project=project,
                repo=repo,
                branch=branch,
                types=types or (),
                updated_since=updated_since,
                include_inactive=include_inactive,
                limit=limit,
                cursor=cursor,
            )
        return page.to_dict()

    @mcp.tool(description=DESCRIPTIONS["memory_contexts"])
    def memory_contexts() -> dict[str, Any]:
        return {"contexts": [asdict(c) for c in bus.contexts()]}

    @mcp.tool(description=DESCRIPTIONS["memory_get"])
    def memory_get(memory_id: str) -> dict[str, Any]:
        with _tool_errors():
            memory = bus.get(memory_id)
            if memory is None:
                raise NotFound(memory_id)
            return memory.to_dict()

    @mcp.tool(description=DESCRIPTIONS["memory_put"])
    def memory_put(
        content: str,
        memory_type: str,
        ctx: Context,
        title: str = "",
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        source: str | None = None,
        supersedes: list[str] | None = None,
    ) -> dict[str, Any]:
        with _tool_errors():
            result = bus.put(
                title=title,
                content=content,
                memory_type=memory_type,
                workspace=workspace,
                project=project,
                repo=repo,
                branch=branch,
                source=source or _client_name(ctx),
                supersedes=supersedes or (),
            )
        return result.to_dict()

    @mcp.tool(description=DESCRIPTIONS["memory_update"])
    def memory_update(
        memory_id: str,
        expected_version: int,
        title: str | None = None,
        content: str | None = None,
        memory_type: str | None = None,
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        with _tool_errors():
            memory = bus.update(
                memory_id,
                expected_version=expected_version,
                title=_unset_if_none(title),
                content=_unset_if_none(content),
                memory_type=_unset_if_none(memory_type),
                workspace=_unset_if_none(workspace),
                project=_unset_if_none(project),
                repo=_unset_if_none(repo),
                branch=_unset_if_none(branch),
                source=_unset_if_none(source),
            )
        return memory.to_dict()

    @mcp.tool(description=DESCRIPTIONS["memory_delete"])
    def memory_delete(memory_id: str) -> dict[str, Any]:
        return {"memory_id": memory_id, "deleted": bus.delete(memory_id)}

    @mcp.tool(description=DESCRIPTIONS["memory_restore"])
    def memory_restore(memory_id: str) -> dict[str, Any]:
        with _tool_errors():
            return {"memory_id": memory_id, "restored": bus.restore(memory_id)}

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
