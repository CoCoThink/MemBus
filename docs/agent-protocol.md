# MemBus Agent Protocol

Status: V1 design, implemented

For agents, tool descriptions are the protocol. This document is the normative source for the MCP server `instructions` and each tool description; the adapter copies them verbatim.

## Server instructions

```text
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
```

## Memory types

| Type | Use for |
|---|---|
| `preference` | how the user wants things done (tools, style, communication) |
| `procedure` | steps or rules to follow ("never edit generated/client.ts") |
| `decision` | a choice made and why; superseded when reversed |
| `fact` | stable information about the user, systems, or environment |
| `incident` | something that went wrong, its cause, and the fix |
| `note` | anything durable that fits nothing above |

## Tool descriptions

**memory_search** — Ranked search over active memories by exact text, words, identifiers, or Chinese phrases. Pass the current workspace/project/repo/branch: matching context ranks first; with strict=false (default) memories from other contexts may follow as fallback, so check each result's context before relying on it. strict=true returns only memories that apply to the given context.

**memory_list** — List active memories that apply to the given context, newest first, without needing a query. Use at session start with types=[preference, procedure, decision]. updated_since=<timestamp> returns only changes since then; include_inactive=true also returns deleted and superseded memories (check `state`). Page with cursor.

**memory_contexts** — List workspace/project/repo combinations in use with memory counts. Call before writing so you reuse existing names exactly.

**memory_get** — Get one memory by ID in any state. If `superseded_by` is set, the memory is outdated; follow that ID.

**memory_put** — Store one durable memory. Identical content in the same context returns the existing memory (created=false). supersedes=[ids] marks older active memories as replaced because the world changed. Returns `similar` near-matches: if one is the same idea, update or supersede it instead of keeping both. memory_type: preference | procedure | decision | fact | incident | note.

**memory_update** — Correct an active memory's wording, type, or context (same idea, not a change in the world — use memory_put with supersedes for that). Requires expected_version from your last read; on version_conflict the error contains the current memory: re-apply your change to it and retry. Omitted fields are unchanged; an empty string clears title, a context field, or source.

**memory_delete** — Soft-delete a memory that is wrong or no longer useful (prefer supersedes when it was replaced by something newer). Recoverable with memory_restore. Deleting a memory that superseded others makes them active again.

**memory_restore** — Undo memory_delete.
