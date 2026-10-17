# MemBus Architecture

Status: implementation baseline

## 1. Thesis

MemBus is a user-owned memory layer. Agent products are replaceable clients.

```text
Claude Code / Codex / Cursor / ChatGPT
                 │
             MCP / CLI
                 ▼
             adapters
                 │
                 ▼
            MemoryService
          ┌──────┴──────┐
          │             │
      repository     retrieval
          │             │
          └──────┬──────┘
                 ▼
              SQLite
                 │
                 ▼
                FTS5
```

SQLite owns canonical memory. FTS is derived. Adapters do not own data.

## 2. Invariants

1. The core works without an LLM, embedding model, network, or API key.
2. Exact lexical evidence can enter retrieval independently.
3. Known scope mismatches are excluded.
4. Local MCP and ChatGPT through Secure MCP Tunnel use the same semantics.
5. New subsystems require measured evidence.

## 3. Storage

Current database state is intentionally small:

```text
memory.db
├── memories
├── aliases
└── memory_fts
```

The canonical memory row contains:

```text
id
title
content
memory_type

scope
workspace
project
repo
branch

source_type
source_ref

created_at
updated_at
deleted_at
```

There is no schema-version table or migration layer yet because MemBus has not been deployed. During development, schema changes may require recreating local/test databases. Versioning is introduced before the first compatibility commitment, not before.

## 4. Read path

```text
query
  ├─ exact full-query substring
  └─ tokenize -> aliases -> FTS5
          │
          ▼
     candidate union
          │
          ▼
     scope eligibility
          │
          ▼
0.80 lexical + 0.20 scope
          │
          ▼
        Top-K
```

`search_many` runs several formulations, unions memory IDs, keeps the best score per memory, and sorts once.

## 5. Write path

```text
request
  ↓
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

Logical delete only sets `deleted_at`.

All application writes use `BEGIN IMMEDIATE`. This remains because multi-process testing reproduced deferred-transaction lock-upgrade failures.

## 6. Scope

Supported scopes:

```text
global
workspace
project
repo
branch
```

Known workspace/project/repo/branch mismatches are ineligible.

Scope also contributes a small ranking preference:

```text
global     0.40
workspace  0.60
project    0.75
repo       0.90
branch     1.00
```

## 7. Operations

Stable service/MCP operations:

```text
put
get
update
delete
search
search_many
```

Current extra operational capabilities:

- deterministic aliases
- SQLite backup API
- `membus doctor`
- isolated Golden Retrieval Evaluation
- WAL + multi-process writer stress coverage
- Secure MCP Tunnel deployment using the same stdio MCP server

## 8. Deferred complexity

Not in the current architecture:

- schema migration/version compatibility
- mutation event ledger
- runtime retrieval logging
- importance/confidence/trust scores
- temporal validity/supersession
- embeddings/vector DB
- graph DB
- LLM query rewrite/rerank
- automatic extraction/consolidation
- ACL/multi-user gateway
- distributed storage

Add any of these only when a concrete failure justifies it.

## 9. Evidence gates

A proposal for new complexity should state:

```text
Observed failure:
Failure rate:
Why current design cannot fix it:
Expected gain:
Operational cost:
Rollback path:
```

The Golden Retrieval Evaluation is the current gate for retrieval changes.
