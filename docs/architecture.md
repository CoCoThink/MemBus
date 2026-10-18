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
source
created_at
updated_at
deleted_at
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

With `strict=False`, context remains a preference; conflicting candidates may be returned. With `strict=True`, filtering occurs in SQL before LIMIT and requires stored context values to be either absent or equal to explicitly supplied query dimensions. Missing query dimensions accept only absent stored dimensions. MCP defaults to strict; Python/CLI default to broad. This is applicability filtering, not ACL.

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

Delete is recoverable: `delete` sets `deleted_at`, `restore` clears it. FTS triggers index only live memories. Every write updates `updated_at` while retaining `created_at`; `source` is only a label.

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
- edit revision history
- runtime retrieval logs
- batch retrieval
- calibrated ranking
- query rewrite/rerank
- vector or graph indexes
- automatic extraction
- ACL/multi-user gateway
- distributed storage
