# MemBus Requirements

Status: V1 development baseline

## 1. Goal

MemBus keeps long-term agent memory in a user-owned local store so Claude Code, Codex, Cursor, ChatGPT, and future clients can share it without making any agent vendor the system of record.

## 2. Required principles

- local-first SQLite
- exact/lexical retrieval works without embeddings
- FTS is derived from canonical memory
- scope prevents cross-project leakage
- adapters call a shared service layer
- the core requires no LLM or hosted service
- complexity is added only after measured failure

## 3. Canonical memory

A memory contains:

- stable ID
- title
- content
- memory type
- scope
- optional workspace/project/repo/branch
- optional source type/reference
- created/updated timestamps
- optional deleted timestamp

No generic importance, confidence, trust, temporal validity, or supersession fields exist in V1.

## 4. Memory types

```text
fact
decision
preference
procedure
incident
failure
note
```

## 5. Scope

```text
global
workspace
project
repo
branch
```

Required fields:

| scope | required |
|---|---|
| global | none |
| workspace | workspace |
| project | project |
| repo | repo |
| branch | repo + branch |

Known parent mismatches are excluded.

## 6. Core operations

```text
put
get
update
delete
search
search_many
```

Delete is logical through `deleted_at`.

## 7. Retrieval

Candidate sources:

1. exact/full-query substring matching
2. FTS5/BM25 with deterministic aliases

Ranking:

```text
score =
    0.80 * lexical
  + 0.20 * scope
```

`search_many` unions candidates and keeps the best score per memory.

## 8. Persistence

Requirements:

- SQLite WAL
- busy timeout
- bounded retry for lock contention
- `BEGIN IMMEDIATE` for all application writes
- SQLite backup API
- FTS synchronization through triggers

No schema migration compatibility is required before the first deployment.

## 9. Health and evaluation

`membus doctor` verifies:

- initialization
- WAL
- FTS5
- SQLite integrity
- FTS/canonical consistency
- write readiness

Golden evaluation tracks:

- Recall@5
- Recall@10
- MRR
- Precision@5
- zero-result rate
- wrong-scope hit rate
- P50/P95 latency

## 10. Explicit non-goals

Not required now:

- migration/version compatibility
- event history
- runtime retrieval logging
- embeddings/vector DB
- graph DB
- LLM query rewrite/rerank
- automatic ingestion/extraction/consolidation
- temporal lifecycle
- ACL/multi-user tenancy
- distributed storage

## 11. Acceptance criteria

V1 development baseline is healthy when:

1. a fresh DB initializes idempotently
2. CRUD and logical delete work
3. exact identifiers and technical terms retrieve reliably
4. wrong project/repo memories are excluded
5. aliases and multi-query retrieval work
6. concurrent readers and multi-process writers do not corrupt the DB
7. backup/restore works in WAL mode
8. core tests require no network/model credentials
9. MCP and CLI use the same service semantics
10. Golden retrieval gates pass
