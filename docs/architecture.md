# MemBus Architecture

Status: development baseline

## Core

```text
Claude Code / Codex / Cursor / ChatGPT
                 │
             MCP / CLI
                 ▼
               MemBus
                 │
                 ▼
              SQLite
           ┌─────┼─────┐
           │     │     │
       memories aliases FTS5
```

There is one runtime core object. MCP and CLI are adapters over the same `MemBus`.

## Canonical memory

```text
id
title
content
memory_type
workspace
project
repo
branch
```

Context specificity is implicit:

```text
branch set     -> branch-specific
repo set       -> repo-specific
project set    -> project-specific
workspace set  -> workspace-specific
none set       -> global
```

A branch requires a repo.

## Retrieval

```text
query
  ├─ exact substring
  └─ tokenize -> aliases -> FTS5
          │
          ▼
context preference tiers
          │
          ▼
matching/global context first
conflicting context as fallback
          │
          ▼
exact first within tier
then FTS BM25
          │
          ▼
Top-K
```

Context is soft retrieval metadata. Query-provided context prefers memories that match those dimensions, while conflicting memories remain fallback candidates. A missing query context dimension does not filter anything.

There is no numeric scoring layer, recency signal, title boost, explain mode, or multi-query fusion. Context uses discrete preference tiers only.

## Writes

```text
validate
  ↓
BEGIN IMMEDIATE
  ↓
SQLite mutation
  ↓
FTS trigger
  ↓
commit
```

Delete is physical.

## Runtime responsibilities

`MemBus` owns:

- schema initialization
- SQLite connections
- WAL
- write transactions and retry
- CRUD
- aliases
- exact retrieval
- FTS retrieval
- context filtering
- backup

There is no Database, Repository, Store interface, Service layer, or separate retrieval subsystem.

## Deferred complexity

Add new machinery only for a measured failure:

- compatibility migrations
- history/soft delete
- runtime retrieval logs
- batch retrieval
- calibrated ranking
- query rewrite/rerank
- vector or graph indexes
- automatic extraction
- ACL/multi-user gateway
- distributed storage
