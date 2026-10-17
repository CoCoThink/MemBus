# MemBus Architecture

Status: implementation baseline

## 1. Thesis

MemBus is a user-owned memory layer. Agent products are clients, not the system of record.

```text
Claude Code / Codex / Cursor / ChatGPT / future agents
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
                 canonical SQLite
                         │
                         ▼
                       FTS5
                 (derived index)
```

The canonical database owns memory. Adapters and retrieval indexes are replaceable.

## 2. Invariants

### A1. No adapter owns memory

MCP, CLI, Secure MCP Tunnel, or a future transport may disappear without invalidating the store.

### A2. No index owns memory

FTS5 and any future vector/graph index are derived from canonical records.

### A3. Core does not require AI inference

Storage and baseline retrieval work without an LLM, embedding model, network, or API key.

### A4. Scope is a correctness boundary

Known workspace/project/repo/branch mismatches are excluded before a high text score can make them authoritative.

### A5. Automatic memory formation stays above the core

A future extractor may propose memories, but it must call the same validated service API.

### A6. Retrieval stays observable

Ranking behavior must be explainable and regression-tested.

### A7. Transport does not change semantics

```text
local client ── stdio ──────────────┐
                                    ├──> membus-mcp -> MemoryService
ChatGPT Web ─ Secure MCP Tunnel ────┘
```

ChatGPT Web is another MCP client, not a second memory system.

## 3. Component boundaries

### Canonical store

SQLite stores:

- canonical memories
- deterministic aliases
- mutation events
- optional retrieval logs
- schema version

It does not store model state, embeddings, tunnel credentials, or task-management state.

### Repository

The repository owns SQL and persistence.

Rules:

- adapters never issue SQL
- service code does not depend on table layout
- writes use explicit transactions
- application writes default to `BEGIN IMMEDIATE`

### Retrieval

Current responsibilities:

- normalize query text
- exact/full-query candidate retrieval
- deterministic alias expansion
- FTS5/BM25 candidate retrieval
- scope eligibility/affinity
- lexical + scope ranking
- multi-query candidate union
- explanation signals

There is no LLM, vector, graph, recency decay, importance score, confidence score, or trust score in the default path.

### Service

Stable operations:

```text
put
get
update
delete
search
search_many
```

The service validates memory type, scope structure, mutation fields, and content.

### Adapters

Current adapters/interfaces:

- MCP stdio
- CLI

Secure MCP Tunnel forwards to the same stdio MCP server.

## 4. Canonical memory shape

Current schema version 2 intentionally contains only fields with runtime meaning:

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

A memory is active when `deleted_at IS NULL`.

The schema does not reserve generic future fields such as importance, confidence, trust, validity windows, or supersession links. They can be added by migration when their semantics exist.

## 5. Read path

```text
query
  │
  ├── exact full-query substring
  │
  └── tokenize -> aliases -> FTS5
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
Top-K + optional explanation
```

Exact technical evidence can independently enter the candidate pool.

Examples:

- `BUG-18472`
- `generated/client.ts`
- `reconcilePayment`
- `max_connections`

### Multi-query

```text
Q1 ──┐
Q2 ──┼──> union by memory ID -> keep best score -> Top-K
Q3 ──┘
```

There is no extra hit-count bonus.

## 6. Write path

```text
client request
      │
      ▼
validation
      │
      ▼
BEGIN IMMEDIATE
      ├── canonical mutation
      ├── FTS trigger
      └── event append
      │
      ▼
commit
```

Logical delete sets `deleted_at`. It does not remove the canonical row.

## 7. Scope model

Scopes:

```text
global
workspace
project
repo
branch
```

Scope has two jobs:

1. reject known mismatches
2. weakly prefer more specific applicable context

Current affinity:

```text
global     0.40
workspace  0.60
project    0.75
repo       0.90
branch     1.00
```

A known mismatch is ineligible, not merely lower ranked.

## 8. Persistence and concurrency

SQLite uses:

- WAL
- foreign keys
- busy timeout
- bounded retry for transient lock contention

All application writes start with:

```sql
BEGIN IMMEDIATE;
```

This decision came from an actual multi-process failure with deferred transactions. Since SQLite has one writer anyway, acquiring the writer reservation at transaction start makes contention predictable without sacrificing useful writer parallelism.

Read-only operations do not open write transactions.

## 9. FTS and indexes

FTS5 is derived state synchronized by SQLite triggers.

The canonical schema keeps only the additional index currently needed for event lookup:

```text
idx_events_memory_id
```

Other metadata indexes are added only after query plans or measurements show a need.

## 10. Backup and health

Backups use SQLite's backup API.

`membus doctor` owns application/data health:

- schema
- WAL
- foreign keys
- FTS5
- integrity
- canonical/FTS consistency
- write readiness

`tunnel-client doctor` owns tunnel/control-plane health.

## 11. Schema evolution

The database stores a schema version.

Rules:

1. fresh DBs are created directly at the current schema
2. older supported DBs use ordered migrations
3. newer DBs are rejected by older binaries
4. canonical data is never moved exclusively into a derived index
5. destructive contractions preserve meaningful removed data in audit history when practical

Schema v2 is the first real migration and deliberately removed unused scoring/lifecycle fields plus the redundant status column.

## 12. Evaluation

The Golden Retrieval Evaluation is the gate for retrieval complexity.

Current default path stays lexical-first until measured failures justify more machinery.

A proposed subsystem should answer:

```text
Observed failure:
Failure rate:
Why the current path cannot fix it:
Expected metric gain:
Operational cost:
Rollback path:
```

## 13. Evidence gates

Add deterministic/LLM query rewriting when lexical misses are repeatedly recoverable by reformulation.

Add reranking when the correct memory is already retrieved but commonly ranks too low.

Add an embedding side index when lexical + aliases + reformulation still miss materially relevant memories.

Add temporal validity and supersession when stale/conflicting facts become a recurring error source.

Add graph traversal when answers repeatedly require multi-hop relationships.

Add ACL/identity when different users or clients actually require different access.

Add automatic extraction when explicit writes become a measured bottleneck and memory pollution can be evaluated.

Add distributed/vector-database infrastructure only when SQLite plus derived indexes reaches a demonstrated capacity or availability limit.

These are possible responses to evidence, not promised architecture layers.
