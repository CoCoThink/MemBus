# MemBus Requirements

Status: V1 implementation baseline  
Scope: user-owned long-term memory infrastructure for AI agents

## 1. Problem

Agent products increasingly keep memory inside their own product boundary. That fragments long-term state and creates vendor lock-in.

MemBus treats agents as replaceable clients:

```text
Claude Code / Codex / Cursor / ChatGPT / future agents
                         │
                    MCP / CLI
                         ▼
                       MemBus
                         ▼
               user-owned SQLite store
```

The V1 goal is a small, reliable, inspectable memory core. It is not an autonomous cognitive architecture.

## 2. Principles

### P1. User sovereignty

Canonical memory remains usable if any agent vendor, account, protocol, or retrieval subsystem disappears.

### P2. Local first

V1 works with local SQLite and requires no hosted service.

### P3. Lexical first

Exact matching and SQLite FTS5/BM25 are sufficient for baseline correctness. Embeddings are optional future indexes.

### P4. Derived indexes are disposable

FTS, vector indexes, graph projections, and caches must be rebuildable from canonical records.

### P5. Scope is a correctness boundary

Workspace/project/repo/branch mismatches must not leak unrelated memory into the result set.

### P6. Read/write separation

Retrieval and future automatic memory formation are separate pipelines.

### P7. Explainability

The caller can inspect the lexical and scope signals that produced a result.

### P8. Progressive complexity

A feature is added only when a measured failure justifies it.

## 3. Users and non-goals

Primary target:

- one technical user
- several agent clients owned by that user
- one canonical memory store

V1 does not target multi-user SaaS.

V1 does not require:

- embeddings
- vector DB
- graph DB
- LLM query rewriting
- LLM reranking
- automatic conversation ingestion
- automatic memory extraction
- autonomous consolidation
- multi-user ACL
- distributed storage
- hosted control plane

## 4. Canonical memory

A V1 memory contains:

- stable ID
- title
- content
- memory type
- scope
- optional workspace/project/repo/branch
- optional source type/reference
- created/updated timestamps
- optional deleted timestamp

Fields without current runtime semantics do not belong in the canonical row.

In particular, V1 does not carry generic:

```text
importance
confidence
trust
valid_from
valid_until
supersedes_id
```

Those concepts may return later with explicit semantics and evaluation evidence.

## 5. Memory types

V1 supports:

```text
fact
decision
preference
procedure
incident
failure
note
```

## 6. Scope

V1 supports:

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

Known parent mismatches are ineligible, not merely lower ranked.

## 7. Core operations

The service provides:

```text
put
get
update
delete
search
search_many
```

Delete is logical: `deleted_at` is set and the canonical row remains auditable.

The MCP adapter exposes the same six operations and does not own storage semantics.

## 8. Retrieval

Search must work with SQLite + FTS5 only.

Candidate sources:

1. exact/full-query substring matching
2. FTS5 lexical matching with deterministic alias expansion

Exact identifiers such as issue IDs, paths, symbols, and config keys must be able to enter the candidate set independently.

V1 ranking is intentionally small:

```text
score =
    0.80 * lexical
  + 0.20 * scope
```

No recency, importance, confidence, or trust signal is used by default.

`search_many` unions results from several lexical formulations and keeps the best score per memory. It does not add an arbitrary multi-query bonus.

## 9. Provenance and audit

A memory may retain:

```text
source_type
source_ref
```

Mutations append an event in the same transaction.

Normal events:

```text
created
updated
deleted
```

Events should not duplicate full memory content when a compact changed-field payload is enough.

External content is not automatically durable memory. Any future automatic extraction layer must call the same validated service contract.

## 10. Retrieval logging

Retrieval logging is optional.

Modes:

```text
off
metadata-only
full
```

A log records only:

- optional query text
- scope context
- candidate count
- returned IDs
- latency
- timestamp

There is no generic debug blob in the canonical schema.

## 11. Persistence and concurrency

V1 uses SQLite WAL.

Requirements:

- foreign keys enabled
- busy timeout
- bounded retry for transient lock contention
- all application writes use `BEGIN IMMEDIATE`
- read-only operations do not open write transactions

The `BEGIN IMMEDIATE` rule is based on a reproduced multi-process lock-upgrade failure, not only on theory.

## 12. FTS consistency

Canonical title/content writes must synchronize FTS transactionally through SQLite triggers.

FTS is derived state and may be rebuilt from `memories`.

## 13. Backup and health

Backups use SQLite's backup API, not raw copying of only the WAL main file.

`membus doctor` verifies:

- schema compatibility
- WAL
- foreign keys
- FTS5
- SQLite integrity
- FTS/canonical consistency
- write readiness

## 14. Schema evolution

Schema changes are versioned.

Startup must:

1. create a fresh DB directly at the current schema
2. migrate older supported schemas
3. reject a DB newer than the running binary understands

Current schema version is 2.

The v1 -> v2 migration intentionally removed unused scoring/lifecycle fields and the redundant `status` column. Non-default removed v1 metadata is retained once in the event ledger for auditability.

## 15. Evaluation

The repository maintains an isolated Golden Retrieval Evaluation.

Required metrics:

- Recall@5
- Recall@10
- MRR
- Precision@5
- zero-result rate
- wrong-scope hit rate
- P50/P95 latency

The golden set is a regression baseline and should become harder as real misses are observed.

## 16. Evolution gates

Add query rewrite when lexical misses are regularly recovered by reformulation.

Add a reranker when relevant memories are usually in the candidate set but rank too low.

Add embeddings only when lexical + alias + query reformulation still misses materially relevant memories.

Add temporal validity/supersession only when stale/conflicting facts become a measured problem.

Add graph traversal only when multi-hop relationship questions recur.

Add ACL/identity only when different human users or clients need different access.

Add automatic extraction only when explicit durable writes become a measured maintenance bottleneck.

Add distributed/vector-database infrastructure only when SQLite + derived side indexes hit a demonstrated capacity or availability limit.

## 17. V1 acceptance criteria

V1 is acceptable when:

1. fresh and legacy databases initialize/migrate safely
2. CRUD + logical delete + event audit work
3. exact identifiers and technical terms retrieve reliably
4. wrong project/repo memories are excluded
5. score explanations are reproducible
6. multi-query retrieval works without model calls
7. concurrent readers and multi-process writers work without corruption
8. backup/restore works under WAL
9. core tests run without network/model credentials
10. MCP remains an adapter over the same core used by CLI and future transports
