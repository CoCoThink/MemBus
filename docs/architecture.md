# MemBus Architecture

Status: Draft architecture baseline  
Audience: maintainers, contributors, adapter authors

## 1. Architectural thesis

MemBus is not a vendor-specific agent memory feature. It is a user-owned memory infrastructure layer.

```text
                Agent / Client Layer
 Claude Code | Codex | Cursor | Kimi | Grok | ...
                         │
                  MCP / CLI / HTTP
                         │
                         ▼
                  Adapter Layer
                         │
                         ▼
                   Memory Core
              ┌──────────┼──────────┐
              │          │          │
           storage    retrieval   policy
              │          │          │
              └──────────┼──────────┘
                         ▼
                Canonical SQLite
                         │
              ┌──────────┼──────────┐
              │          │          │
             FTS     optional      optional
                     vector         graph
                     index          index
```

The canonical database is the system of record. Every search index is derived and replaceable.

## 2. Architecture invariants

The following rules are stronger than implementation choices.

### A1. No adapter owns memory

MCP, CLI, HTTP, or an IDE plugin may disappear without invalidating the store.

### A2. No retrieval engine owns memory

FTS5, HNSW, an embedding model, or a graph index may be rebuilt or replaced without changing canonical records.

### A3. Core does not depend on AI inference

The storage and baseline retrieval system must work without network access, API keys, or an LLM.

### A4. Scope is resolved before or during candidate selection

A high lexical score from the wrong project cannot be allowed to silently become authoritative context.

### A5. Automatic writes are a higher-level feature

The core accepts validated memory operations. Future extraction/reflection modules produce candidates and call the same service contract.

### A6. Retrieval must remain observable

A ranking change must be debuggable from logged signals and a reproducible evaluation set.

## 3. Component boundaries

### 3.1 Canonical store

V1: SQLite.

Responsibilities:

- canonical memory rows
- memory metadata and scope
- provenance
- aliases/entities needed by deterministic retrieval
- mutation event ledger
- retrieval logs
- schema versioning

Not responsibilities:

- model inference
- embedding generation
- MCP protocol
- task management

### 3.2 Repository layer

The repository layer owns SQL and transaction-safe persistence.

Rules:

- adapters never issue SQL
- service layer does not know FTS implementation details
- mutations use explicit transactions
- read-modify-write flows may use `BEGIN IMMEDIATE`

### 3.3 Retrieval engine

V1 responsibilities:

- normalize a query
- optional deterministic alias expansion
- execute FTS5/BM25 retrieval
- apply scope eligibility/affinity
- combine scoring signals
- return explanation fields
- support multi-query candidate union

Future responsibilities may include:

- query rewrite
- vector retrieval
- RRF
- graph expansion
- learned reranking

### 3.4 Service layer

Provides stable domain operations:

```text
put
get
update
delete
search
search_many
```

The service validates:

- memory type
- scope
- ID semantics
- mutation semantics
- source metadata

### 3.5 Adapter layer

Adapters translate external protocols to the service contract.

Initial target:

- MCP

Future adapters:

- CLI
- HTTP
- Python SDK
- Unix socket
- editor integrations

Adapters must be stateless where feasible.

## 4. Read path

V1:

```text
query
  │
  ▼
normalize
  │
  ├── exact/identifier terms
  └── alias expansion
  │
  ▼
resolve scope
  │
  ▼
FTS5 candidate retrieval
  │
  ▼
scope / recency / importance / trust scoring
  │
  ▼
Top-K
  │
  ├── result
  └── explanation
```

Batch search:

```text
Q1 ──┐
Q2 ──┼──> candidate union / fusion ──> rerank ──> Top-K
Q3 ──┘
```

V1 deliberately does not require an LLM between the caller and FTS.

## 5. Write path

V1 explicit write path:

```text
client request
      │
      ▼
validation
      │
      ▼
transaction
      ├── canonical mutation
      ├── FTS synchronization
      └── event ledger append
      │
      ▼
commit
```

Future autonomous path:

```text
conversation / source event
          │
          ▼
 candidate extraction
          │
          ▼
 existing-memory lookup
          │
          ▼
 memory gate
   ┌──────┼───────┐
   │      │       │
 dedup conflict durable?
   │      │       │
   └──────┼───────┘
          ▼
 approved candidate
          │
          ▼
      core service
```

The future extractor is not allowed to bypass the core service.

## 6. Retrieval scoring

V1 starts with an intentionally simple transparent model:

```text
final_score =
    w_lexical    * lexical_score
  + w_scope      * scope_score
  + w_recency    * recency_score
  + w_importance * importance
  + w_trust      * trust
```

Initial default weights:

```text
lexical    0.60
scope      0.15
recency    0.10
importance 0.10
trust      0.05
```

These are defaults, not truths. They must eventually be tuned with a golden retrieval set.

### Why lexical is dominant in V1

Coding memory contains high-value literal identifiers:

- symbols
- paths
- issue IDs
- error codes
- configuration keys
- command names
- service names

A semantic gate must never suppress exact evidence such as `BUG-18472`.

## 7. Scope model

Supported V1 scopes:

```text
global
workspace
project
repo
branch
```

A record's scope determines which contextual fields are meaningful.

Example:

```yaml
scope: repo
workspace: work
project: payments
repo: payment-api
branch: null
```

Resolution should favor the most specific applicable context while allowing inherited global/project knowledge.

A future policy may encode scope as:

```text
global
  ↓
workspace
  ↓
project
  ↓
repo
  ↓
branch
```

but V1 keeps eligibility rules explicit rather than implementing a general namespace algebra.

## 8. Persistence and concurrency

SQLite configuration:

- WAL journal mode
- foreign keys ON
- busy timeout
- explicit transactions
- bounded retry for transient `SQLITE_BUSY`

For mutations that perform read-before-write decisions:

```sql
BEGIN IMMEDIATE;
```

should be preferred to avoid a deferred-transaction lock upgrade race.

Ordinary independent inserts/reads may use normal transactions where safe.

## 9. Backup model

With WAL enabled, raw copying of only the main database file can produce an incomplete snapshot.

Primary supported backup path:

```text
SQLite source connection
       │
       └── sqlite3 backup API
                 │
                 ▼
          consistent backup DB
```

Derived indexes need not be backed up if they are reproducible.

## 10. Schema evolution

The database stores a schema version.

Rules:

1. migrations are ordered and deterministic
2. migrations are transactional where SQLite allows
3. startup refuses a database newer than the running binary understands
4. migrations never make a derived index the only copy of canonical content
5. destructive migration requires an explicit compatibility decision

## 11. Evolution roadmap

The roadmap is a sequence of evidence gates, not a feature checklist.

### V0: Files + grep

Use when the dataset is tiny and direct textual inspection is enough.

Adds almost no infrastructure.

Upgrade when ranking and filtering become a recurring problem.

### V1: SQLite + FTS5/BM25

Adds:

- structured canonical records
- ranking
- transactions
- auditability

Needed when grep produces too many unordered hits.

### V2: Scope / metadata / types

Adds:

- cross-project correctness
- filtered retrieval
- typed memories

Needed as soon as multiple projects or workspaces can contain conflicting facts.

### V3: Alias / entity normalization

Adds:

- deterministic synonym handling
- mixed Chinese/English technical vocabulary
- stable entity naming

Needed when misses are caused by `pg/postgres/PostgreSQL`, `prod/production/线上`, etc.

### V4: Query rewrite / multi-query

Adds:

- semantic understanding at query time without requiring a vector DB
- parallel lexical probes

Needed when manual query reformulation reliably recovers missed memories.

### V5: Reranking + formal evaluation

Adds:

- Top-K quality improvement
- measurable architecture decisions

Needed when relevant memories are usually retrieved but rank too low.

### V6: Embedding side index

Adds:

- paraphrase recall
- abstract semantic similarity
- cross-language semantic retrieval

Cost:

- embedding model/version lifecycle
- index build and migration
- memory footprint
- new failure modes

Only needed when an evaluation set proves lexical/query-rewrite recall is inadequate.

The vector index remains derived:

```text
SQLite canonical data
        │
        └── rebuild → HNSW/vector index
```

### V7: Temporal lifecycle and conflicts

Adds:

- `valid_from`
- `valid_until`
- `supersedes`
- canonical/outdated/conflict state

Needed once long-lived facts change often enough that stale memory becomes dangerous.

### V8: Entity graph / multi-hop retrieval

Adds relational expansion:

```text
payment-service
   ├── uses → PostgreSQL
   ├── deployed_on → prod-cluster
   └── depends_on → provider-X
```

Needed when answers commonly require traversing relationships rather than retrieving one record.

### V9: Gateway / ACL

Adds:

- client identity
- namespace permissions
- audit policy
- least privilege

Needed when multiple agents should not all see the same memory domains.

### V10: Automatic extraction / consolidation

Adds:

- candidate extraction
- memory gating
- dedup
- conflict detection
- promotion

Needed only when explicit write workflows become a maintenance bottleneck.

### V11: Vector DB / distributed architecture

Adds:

- sharding
- replication
- multi-node availability
- large-scale vector serving

Needed only when local SQLite + side indexes become a measured capacity or availability bottleneck.

## 12. Complexity budget

Before adding any major subsystem, the proposal should state:

```text
Observed failure:
Measurement window:
Failure rate:
Why current architecture cannot fix it:
Proposed component:
Expected metric improvement:
Operational cost:
Rollback path:
```

Example:

```text
Observed failure:
41 / 200 golden queries miss the correct memory at Recall@10
because query and memory have almost no lexical overlap.

Query rewrite recovers 17/41.
A vector side index recovers 36/41.

Decision:
Introduce an optional embedding index because Recall@10
improves from 79.5% to 97.5%.
```

This is the standard for justified complexity.

## 13. Intended steady-state “sweet spot”

For a personal coding memory bus, the likely long-term sweet spot is:

```text
                    adapters
                       │
                    MemBus
                       │
          ┌────────────┼────────────┐
          │            │            │
       SQLite         FTS5       optional HNSW
          │            │            │
          └────────────┼────────────┘
                       │
               retrieval engine
                       │
        scope / alias / metadata
                       │
              multi-query / fusion
                       │
                  reranker
```

with:

- provenance
- event history
- retrieval logs
- temporal lifecycle
- a golden evaluation set

Graph databases, hosted vector databases, and autonomous consolidation remain optional extensions rather than architectural prerequisites.
