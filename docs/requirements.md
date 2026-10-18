# MemBus Requirements

Status: V1 development baseline

## Goal

Provide one user-owned local memory store shared by multiple AI clients.

## Required behavior

- SQLite is canonical.
- MCP and CLI call the same `MemBus` core.
- Exact identifiers can be retrieved without embeddings.
- FTS5/BM25 provides lexical retrieval.
- Aliases provide deterministic query expansion.
- Workspace/project/repo/branch context supports broad preference (`strict=False`) and strict applicability filtering (`strict=True`); MCP defaults to strict, Python/CLI to broad.
- The runtime requires no LLM, hosted service, or embedding model.
- Writes remain safe under the tested multi-process SQLite workload.

## Canonical memory

A memory contains:

- stable ID
- title
- content
- memory type
- optional workspace/project/repo/branch
- optional unverified source label
- UTC created_at and updated_at timestamps
- nullable deleted_at for recoverable deletion

A branch requires a repo.

## Operations

```text
put
get
update
delete
restore
search
```

Delete is soft: normal reads/search exclude deleted rows; restore reinstates them and FTS entries.

## Retrieval

Candidate sources:

1. exact full-query substring matching
2. FTS5/BM25 after alias expansion

Ordering:

1. exact matches
2. remaining FTS results in BM25 order

There is no public score or explanation object.

## Persistence

Required:

- WAL
- busy timeout
- bounded retry
- `BEGIN IMMEDIATE` for writes
- SQLite backup API
- FTS synchronization triggers

Before first deployment there is no schema compatibility layer.

## Health and evaluation

`membus doctor` checks WAL, FTS5, SQLite integrity, FTS consistency, and write readiness.

Repository tooling tracks:

- Recall@5
- Recall@10
- MRR
- zero-result rate
- context-preference error rate
- P50/P95 latency

## Non-goals

Not required now:

- authenticated provenance / revision history
- schema migrations
- runtime retrieval logs
- batch/search-many retrieval
- relevance-score calibration
- embeddings/vector DB
- graph DB
- LLM query rewrite/rerank
- automatic ingestion/extraction/consolidation
- ACL/multi-user tenancy
- distributed storage
