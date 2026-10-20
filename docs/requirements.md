# MemBus Requirements

Status: implemented, schema 2

## Goal

Provide one user-owned local memory bus shared by multiple AI clients.

"Bus" means more than a store: any connected agent can discover what other agents recorded, keep shared memory current, and avoid duplicating or silently overwriting each other's work.

## Trust model

Single user. Every connected client (Claude Code, Codex, Cursor, ChatGPT via Secure MCP Tunnel, ...) is trusted by the user and receives the full tool surface. There is no per-client permission mode, ACL, or authenticated provenance.

Trusting a client is not trusting everything it writes: an agent may have read injected content. The agent contract therefore frames memories as agent-written notes that never alone justify risky actions, and MCP records the writing client as `source` instead of accepting it from the agent.

## Required behavior

- SQLite is canonical; every index is derived and rebuildable.
- MCP and CLI call the same `MemBus` core.
- The runtime requires no LLM, hosted service, or embedding model.
- Exact identifiers (paths, symbols, issue IDs) are retrievable without embeddings.
- Lexical retrieval covers English morphology (stemming) and Chinese text without a segmenter.
- Agents can discover saved memories without knowing their original context, then verify which memories apply to the current task.
- Agents can synchronize latest states since a revision, including soft deletions, supersessions, and scope changes. Intermediate revisions need not be retained.
- Concurrent agents cannot silently overwrite each other's updates.
- Retried or repeated identical writes do not create duplicates.
- Changed facts/decisions are replaced explicitly, and the replaced memory stops appearing as current. If the replacement is deleted, the replaced memory can be restored explicitly.
- Agents without persistent state can browse what changed in a context since a point in time (best effort).
- Writes remain safe under the tested multi-process SQLite workload.
- The agent-facing contract (tool descriptions, server instructions) states when and how to read and write memory.

## Canonical memory

A memory contains:

- stable ID
- title (≤ 200 characters) and content (1–4000 characters)
- memory type: `fact | decision | preference | procedure | incident | note`
- optional workspace/project/repo/branch context (branch requires repo)
- optional source label: over MCP, the client that last wrote it (self-reported by the client software, not authenticated); free-form from CLI/Python
- content hash (dedup key)
- globally allocated revision, used for concurrency checks and synchronization
- UTC `created_at` and `updated_at`
- nullable `deleted_at` (recoverable deletion)
- nullable `superseded_by` (ID of the memory that replaced it)

A memory is in exactly one state:

```text
active      deleted_at IS NULL AND superseded_by IS NULL
superseded  deleted_at IS NULL AND superseded_by IS NOT NULL
deleted     deleted_at IS NOT NULL
```

Only active memories are searched, listed by default, updated, or deduplicated against.

## Operations

```text
put        create or deduplicate; supersedes targets carry expected_revision
get        read any state by ID
update     correct an active memory; requires expected_revision
delete     soft-delete at expected_revision; leaves predecessors superseded
restore    make current again at expected_revision unless a live successor exists
search     ranked lexical retrieval
list       browse with optional known context fields (omitted = unconstrained), newest revision first, with cursor
sync       latest states globally, oldest unseen revision first
contexts   enumerate known workspace/project/repo values
purge      CLI only: irreversible removal; rotate sync generation
```

## Retrieval

Candidate sources, all over active memories:

1. exact full-query substring (trigram index)
2. word FTS5/BM25 with Porter stemming after stopword removal and alias expansion
3. Chinese 3-gram matching (trigram index)
4. last resorts while results are short: word-part substrings (deploy → deployment) and separated Han bigrams

Ordering: known-context conflict tier first (no conflicts before conflicts), then source order, then within-source rank (exact: newest first; FTS: BM25). Strict search is opt-in and requires applicability for all stored dimensions.

No public score or explanation object.

## Persistence

- WAL, busy timeout, bounded retry
- `BEGIN IMMEDIATE` for writes
- SQLite backup API
- a single metadata row holds the global revision counter and sync generation
- all memory mutations allocate revisions inside their write transaction
- FTS synchronization triggers
- no schema compatibility layer before first deployment

## Health and evaluation

`membus doctor` checks WAL, FTS5 (including trigram tokenizer), SQLite integrity, both FTS indexes' consistency with active memories, and write readiness.

Repository tooling tracks Recall@5, Recall@10, MRR, zero-result rate, context-preference error rate, superseded-leak rate, and P50/P95 latency at fixture and 100k-memory synthetic scale.

## Scale target

Personal use: up to 100k memories.

- Keyword, identifier, and typical sentence queries: search P95 under 50 ms (golden queries measure ~26 ms on a laptop at 100k).
- `update`, `delete`, `restore`, `list`, `contexts`: under 10 ms.
- `put` includes the duplicate check: under 100 ms.

Known soft spot: a long natural-language query made only of very common words (BM25 must score tens of thousands of matches) was measured at ~75 ms on the deliberately low-vocabulary synthetic corpus. Revisit with document-frequency term pruning only if real data shows it.

## Non-goals

- per-client permissions / ACL / multi-user tenancy
- authenticated provenance
- edit revision history (supersession and backup cover the real needs; see architecture)
- runtime retrieval logs
- relevance-score calibration
- embeddings/vector DB, graph DB
- LLM query rewrite/rerank
- automatic extraction/consolidation inside MemBus (agents consolidate via `put(supersedes=...)`)
- event history and atomic multi-page snapshots (sync converges on latest states)
- push notifications / subscriptions (agents poll `sync`)
- distributed storage
