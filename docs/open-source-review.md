# Open-source Memory System Review

This document records design lessons, not dependencies to copy wholesale.

Reviewed directions:

- MCP reference `server-memory`
- `RMANOV/sqlite-memory-mcp`
- Mem0
- Graphiti / Cognee

## What MemBus optimizes for

MemBus currently asks only:

1. Is canonical memory user-owned and inspectable?
2. Can it work without embeddings or hosted services?
3. Can exact technical identifiers independently enter retrieval?
4. Can workspace/project/repo/branch context prevent known leakage?
5. Can multiple MCP clients share one store?
6. Are indexes derived and rebuildable?
7. How much unrelated agent-platform machinery is being pulled into the core?

## MCP server-memory

Useful lesson: keep the agent-facing tool surface small and understandable.

Not adopted:

- JSONL as the canonical database
- graph entities/relations as the required internal representation
- substring-only retrieval

## sqlite-memory-mcp

Useful engineering lessons:

- SQLite local durability
- WAL
- explicit transactions
- retry around lock contention
- `BEGIN IMMEDIATE`
- SQLite backup API
- FTS5/BM25
- optional rather than mandatory vector retrieval

Not adopted:

- task/collaboration/product subsystems
- large MCP surface
- contextual scoring stacks before evidence requires them
- provenance/event/temporal machinery in the baseline core

## Mem0

Useful future lessons:

- evaluate memory quality
- consider existing memory before automatic extraction
- avoid blindly duplicating durable facts
- test semantic retrieval rather than assuming it is necessary

Not adopted:

- vector-store-centered ownership
- mandatory embedding retrieval
- hosted memory ownership boundary

## Graphiti / Cognee

These are references only if the problem later becomes temporal, relational, or multi-hop.

They are not baseline dependencies.

## Current MemBus conclusion

The V1 design (see [architecture](architecture.md)) adopts a few more of the lessons above, each tied to a review finding rather than to a reference system:

- from sqlite-memory-mcp: keep SQLite/WAL/FTS5; add a trigram FTS index instead of a segmenter;
- from Mem0: avoid blindly duplicating durable facts — content-hash idempotent put plus advisory `similar`, with the agent (not MemBus) deciding to update or supersede;
- from Graphiti: only the minimal temporal idea — explicit supersession — without validity intervals or a graph.

```text
MCP / CLI
    │
    ▼
  MemBus
    │
    ▼
 SQLite
 ├─ memories   (version, content_hash, superseded_by)
 ├─ aliases
 ├─ memory_fts      (porter)
 └─ memory_trigram  (trigram)
```

Still absent: ranking subsystem, revision history, graph, embedding index, extraction pipeline, adapter-independent service hierarchy.

## Future rule

A new subsystem should be introduced only when a measured failure says what problem it solves and the Golden Retrieval Evaluation shows a meaningful improvement.

The purpose of this review is to prevent both dependency-by-fashion and architecture-by-anticipation.
