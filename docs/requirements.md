# MemBus Requirements

Status: V1 development baseline

## Goal

MemBus provides one user-owned local memory store shared by multiple AI clients.

## Required behavior

- SQLite is canonical.
- MCP and CLI use the same service semantics.
- Exact identifiers can be retrieved without embeddings.
- FTS5/BM25 provides lexical retrieval.
- Aliases provide deterministic query expansion.
- Workspace/project/repo/branch context prevents known cross-context leakage.
- The core requires no LLM, hosted service, or embedding model.
- Writes are safe under the tested multi-process SQLite workload.

## Canonical memory

A memory contains:

- stable ID
- title
- content
- memory type
- optional workspace/project/repo/branch
- optional source type/reference
- created/updated timestamps

No explicit scope field exists. Context specificity is inferred from populated context fields.

A branch requires a repo.

## Operations

```text
put
get
update
delete
search
```

Delete is physical.

## Retrieval

Candidate sources:

1. exact full-query substring matching
2. FTS5/BM25 after deterministic alias expansion

Context rules are applied in SQL:

- if a memory has a context value and the request supplies a different value, exclude it
- a memory with no value for that dimension remains applicable

Ordering:

1. exact matches
2. remaining FTS results in BM25 order

No synthetic numeric score is exposed.

## Persistence

Required:

- WAL
- busy timeout
- bounded busy retry
- `BEGIN IMMEDIATE` for writes
- SQLite backup API
- FTS triggers

No compatibility migrations are required before first deployment.

## Health and evaluation

`membus doctor` checks:

- initialization
- WAL
- FTS5
- SQLite integrity
- canonical/FTS consistency
- write readiness

Golden evaluation tracks:

- Recall@5
- Recall@10
- MRR
- zero-result rate
- wrong-context hit rate
- P50/P95 latency

## Non-goals

Not required now:

- soft delete or restore
- event history
- runtime retrieval logging
- search-many/batch retrieval
- relevance-score calibration
- embeddings/vector DB
- graph DB
- LLM query rewrite/rerank
- automatic ingestion/extraction/consolidation
- ACL/multi-user tenancy
- distributed storage
