# MemBus Architecture

Status: development baseline

## 1. Core

```text
Claude Code / Codex / Cursor / ChatGPT
                 │
             MCP / CLI
                 ▼
            MemoryService
                 │
                 ▼
             SQLiteStore
             ┌───┼────┐
             │   │    │
        memories aliases FTS5
```

SQLite owns canonical memory. MCP and FTS are replaceable edges.

## 2. Canonical data

```text
id
title
content
memory_type
workspace
project
repo
branch
source_type
source_ref
created_at
updated_at
```

Context specificity is implicit:

```text
branch set     -> branch-specific
repo set       -> repo-specific
project set    -> project-specific
workspace set  -> workspace-specific
none set       -> global
```

A branch requires a repo. No separate `scope` column or enum exists.

## 3. Retrieval

```text
query
  ├── exact substring
  └── tokenize -> aliases -> FTS5
          │
          ▼
context mismatch exclusion
          │
          ▼
exact matches first
then FTS BM25 order
          │
          ▼
Top-K
```

A memory is excluded only when both the memory and the request provide a context dimension and their values differ.

There is no numeric score, reranking layer, scope bonus, recency bonus, title boost, or multi-query fusion.

## 4. Writes

```text
validate
  ↓
BEGIN IMMEDIATE
  ↓
canonical mutation
  ↓
FTS trigger
  ↓
commit
```

Delete is physical. The FTS delete trigger removes the corresponding derived row.

## 5. Storage boundary

`SQLiteStore` owns:

- connections
- WAL initialization
- transactions/retry
- backup
- CRUD
- aliases
- exact retrieval
- FTS retrieval
- context filtering

There is no separate Database or Repository abstraction because there is no second persistence backend.

## 6. Operations

```text
put
get
update
delete
search
```

Current operational support:

- SQLite backup
- doctor
- Golden Retrieval Evaluation
- multi-process writer stress coverage
- Secure MCP Tunnel through the same stdio MCP server

## 7. Deferred complexity

Not present until evidence requires it:

- compatibility migrations
- soft delete/history
- runtime retrieval logs
- batch/search-many API
- synthetic relevance scores
- query rewrite/rerank
- vector or graph indexes
- automatic extraction
- ACL/multi-user gateway
- distributed storage
