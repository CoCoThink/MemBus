# MemBus Requirements

Status: Draft for V1 implementation  
Scope: user-sovereign long-term memory infrastructure for AI agents

## 1. Problem statement

AI coding and knowledge-work clients increasingly maintain their own private memory, rules, chat history, and project context. That creates vendor lock-in and fragments the user's long-term state across products and accounts.

MemBus treats agent applications as replaceable clients. Long-term memory is an independent user-owned system of record.

```text
Claude Code / Codex / Cursor / Kimi / Grok / future agents
                         │
                         │ adapter (MCP / CLI / HTTP)
                         ▼
                      MemBus
                         │
                         ▼
              user-owned canonical store
```

The V1 goal is not to build an autonomous cognitive architecture. It is to establish a reliable, inspectable, low-latency local memory core that can evolve without changing ownership of the data.

## 2. Product principles

### P1. User sovereignty

The canonical memory store MUST remain usable if any individual model vendor, agent client, account, protocol, or retrieval subsystem disappears.

### P2. Local-first

V1 MUST work entirely on a local machine with SQLite and without a mandatory cloud service.

### P3. Lexical-first

V1 MUST provide useful retrieval with exact matching and SQLite FTS5/BM25. Embeddings and vector search MUST NOT be required for correctness.

### P4. Derived indexes are disposable

Any index, cache, embedding, graph projection, or materialized retrieval structure MUST be rebuildable from canonical records.

### P5. Scope is a correctness boundary

Workspace/project/repository/branch context MUST participate in candidate eligibility or ranking. Scope MUST NOT be treated as an untrusted free-form tag.

### P6. Read/write separation

Memory retrieval and memory formation MUST be separate pipelines. A future automatic extractor MUST NOT be required by the V1 read path.

### P7. Explainability

A caller SHOULD be able to understand why a memory was returned: lexical score, scope match, recency, importance, trust, and other future signals.

### P8. Progressive complexity

A new subsystem (embeddings, graph, LLM reranker, automatic extraction, vector DB, distributed service) SHOULD be introduced only when a measured failure mode justifies it.

## 3. Primary users

### U1. Single technical user

A developer who wants the same long-term project memory available to multiple AI coding tools.

### U2. Multiple agent clients owned by one user

Several local or remote clients accessing one MemBus store with different current workspaces/projects.

Multi-user SaaS is explicitly outside V1.

## 4. Core use cases

### UC-1: Store an explicit durable memory

Example:

> generated/client.ts is generated from OpenAPI and must never be edited manually.

The caller stores this as a `procedure` scoped to a repository and records the source.

### UC-2: Retrieve relevant prior knowledge

Example query:

> Why did payment settlement previously fail under concurrency?

MemBus searches applicable scopes, ranks textual matches, and returns the best memories with provenance and scoring details.

### UC-3: Retrieve an exact technical identifier

Examples:

- `BUG-18472`
- `reconcilePayment`
- `src/payment/reconcile.ts`
- `max_connections`

Exact and lexical signals must not be gated by semantic similarity.

### UC-4: Use the same memory from different agents

Claude Code writes a project decision. Codex later retrieves it through another adapter/process.

### UC-5: Update a changing fact without silently destroying history

V1 provides explicit update/delete operations and an event ledger. Temporal supersession becomes a later lifecycle feature, but V1 MUST preserve enough event history to evolve toward it.

### UC-6: Explain retrieval

A caller requests `explain=true` and receives the component scores used to rank each result.

### UC-7: Backup and recover

The user can create a consistent SQLite backup while WAL mode is enabled.

## 5. Functional requirements

### FR-001 Canonical memory records

MemBus MUST store memory records containing at least:

- stable ID
- title
- content
- memory type
- scope
- optional workspace/project/repo/branch
- status
- importance
- confidence
- trust
- source type/reference
- validity fields reserved for lifecycle evolution
- created/updated timestamps

### FR-002 Memory types

V1 MUST support at least:

- `fact`
- `decision`
- `preference`
- `procedure`
- `incident`
- `failure`
- `note`

The database SHOULD allow future types without schema redesign.

### FR-003 Scope hierarchy

V1 MUST support:

- `global`
- `workspace`
- `project`
- `repo`
- `branch`

Scope fields MUST be validated consistently.

### FR-004 CRUD

The core service MUST implement:

- `put`
- `get`
- `update`
- `delete`

Deletes in V1 SHOULD default to logical deletion unless a maintenance operation explicitly requests physical deletion.

### FR-005 Search

The core service MUST implement:

- single-query search
- batch/multi-query search

Search MUST work with only SQLite + FTS5.

### FR-006 FTS synchronization

Canonical writes and the FTS index MUST remain transactionally consistent.

### FR-007 Alias support

V1 SHOULD support a small alias table for deterministic expansion such as:

```text
pg -> postgresql
prod -> production
cc -> claude code
```

Alias expansion MUST be optional and observable.

### FR-008 Retrieval scoring

V1 ranking MUST include lexical relevance and MAY include:

- scope affinity
- recency
- importance
- trust

The scorer MUST expose component signals for debugging.

### FR-009 Provenance

Every durable memory SHOULD retain a source type and source reference when available.

### FR-010 Event ledger

Writes MUST append a mutation event sufficient to audit:

- create
- update
- logical delete
- restore (when implemented)

### FR-011 Retrieval log

Searches SHOULD record:

- query
- resolved scope
- candidate IDs or count
- returned IDs
- latency
- optional scoring/debug payload

Logging MUST be configurable so sensitive deployments can reduce retained query text.

### FR-012 Backup

MemBus MUST use SQLite's backup API or an equivalent consistent snapshot mechanism. Raw copying of the main DB file while WAL is active MUST NOT be documented as the primary backup method.

### FR-013 MCP adapter

V1 MUST expose the core through MCP, but the domain/core packages MUST NOT depend on MCP.

Initial MCP-facing operations:

- `memory_search`
- `memory_search_many`
- `memory_get`
- `memory_put`
- `memory_update`
- `memory_delete`

### FR-014 Stable internal service contract

Adapters MUST call an internal service API rather than executing SQL directly.

### FR-015 Schema migrations

Schema evolution MUST be versioned. Startup MUST detect incompatible schema versions rather than silently proceeding.

## 6. Non-functional requirements

### NFR-001 Latency

Initial local targets on ordinary developer hardware:

- P50 retrieval < 20 ms
- P95 retrieval < 50 ms

Targets apply to the local SQLite/FTS retrieval path and exclude remote LLM calls.

### NFR-002 Durability

Committed canonical writes MUST survive process restart.

### NFR-003 Concurrent clients

Multiple agent processes MUST be able to read concurrently. Write behavior MUST use SQLite WAL mode, explicit transaction boundaries, busy timeout/retry, and `BEGIN IMMEDIATE` where a read-modify-write operation requires early writer reservation.

### NFR-004 Inspectability

The canonical store MUST remain understandable through ordinary SQLite tooling.

### NFR-005 Dependency budget

The V1 core SHOULD have no mandatory runtime dependency beyond Python's standard library where feasible. Adapter-specific dependencies MAY be optional extras.

### NFR-006 Portability

The V1 data store MUST be movable as ordinary files and usable without a hosted control plane.

### NFR-007 Testability

Storage, ranking, scope resolution, migrations, and service operations MUST be testable without running an LLM.

## 7. Explicit V1 non-goals

V1 will NOT require:

- embeddings
- vector database
- HNSW
- knowledge graph database
- LLM query rewriting
- LLM reranking
- automatic conversation ingestion
- automatic memory extraction
- autonomous memory consolidation/reflection
- multi-user tenancy
- distributed consensus
- cross-machine live replication
- hosted service
- task manager / kanban / agent debate framework

These may be introduced later only against measured needs.

## 8. Safety and trust requirements

### SR-001 External content is not automatically durable memory

Web pages, repository text, email, issues, documents, and tool output MUST NOT become permanent memory solely because the content asks to be remembered.

### SR-002 Explicit provenance

Automatically proposed future memories MUST identify their originating source.

### SR-003 Trust is distinct from relevance

A high text match from a low-trust source must not be treated as equivalent to a canonical configuration or explicit user rule.

### SR-004 Deletion semantics

Adapters MUST distinguish logical deletion from physical maintenance deletion.

## 9. Evolution gates

A feature is introduced only when a real failure mode crosses a useful threshold.

### Gate G1: Query rewrite

Add query rewrite/multi-query intelligence when lexical misses are frequently recoverable by deterministic or LLM reformulation.

### Gate G2: Reranker

Add a more expensive reranker when the correct memory is commonly in Top-20/Top-50 but not Top-5.

### Gate G3: Embeddings

Add a vector side index only when an evaluation set proves lexical + alias + query rewrite recall is insufficient.

### Gate G4: Temporal lifecycle

Add canonical/superseded/conflict mechanics when stale or contradictory memories become a material source of errors.

### Gate G5: Entity graph

Add graph traversal when answers regularly require multi-hop relationships rather than a single memory.

### Gate G6: ACL/gateway

Add strong client identity/ACL once different clients require materially different namespaces or permissions.

### Gate G7: Automatic extraction

Add autonomous memory candidates only after explicit write workflows are stable and memory pollution can be measured.

### Gate G8: Vector database/distributed architecture

Only consider this when a local side index is a demonstrated capacity, concurrency, or availability bottleneck.

## 10. V1 acceptance criteria

V1 is complete when all of the following are true:

1. A fresh database can be initialized and migrated.
2. A memory can be created, retrieved, updated, logically deleted, and audited.
3. FTS search returns exact identifiers and natural technical terms with stable ranking.
4. Scope filtering prevents an obviously wrong project/repo memory from outranking an applicable one.
5. Search can return score explanations.
6. Batch search performs multiple lexical queries without requiring multiple adapter round-trips.
7. Two independent processes can read the same WAL database; write contention is handled without corruption.
8. A consistent backup can be created and restored.
9. The core test suite runs without network access or model credentials.
10. MCP is only an adapter and can be replaced without migrating canonical data.
