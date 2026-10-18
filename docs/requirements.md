# MemBus Requirements

Status: V1 design, implemented (supersedes the pre-deployment baseline)

## Goal

Provide one user-owned local memory bus shared by multiple AI clients.

"Bus" means more than a store: any connected agent can discover what other agents recorded, keep shared memory current, and avoid duplicating or silently overwriting each other's work.

## Trust model

Single user. Every connected client (Claude Code, Codex, Cursor, ChatGPT via Secure MCP Tunnel, ...) is trusted by the user and receives the full tool surface. There is no per-client permission mode, ACL, or authenticated provenance.

## Required behavior

- SQLite is canonical; every index is derived and rebuildable.
- MCP and CLI call the same `MemBus` core.
- The runtime requires no LLM, hosted service, or embedding model.
- Exact identifiers (paths, symbols, issue IDs) are retrievable without embeddings.
- Lexical retrieval covers English morphology (stemming) and Chinese text without a segmenter.
- Agents can enumerate memories applicable to a context without guessing a query.
- Agents can see what changed since a point in time, including deletions and supersessions.
- Concurrent agents cannot silently overwrite each other's updates.
- Retried or repeated identical writes do not create duplicates.
- Changed facts/decisions are replaced explicitly, and the replaced memory stops appearing as current.
- Writes remain safe under the tested multi-process SQLite workload.
- The agent-facing contract (tool descriptions, server instructions) states when and how to read and write memory.

## Canonical memory

A memory contains:

- stable ID
- title (≤ 200 characters) and content (1–4000 characters)
- memory type: `fact | decision | preference | procedure | incident | note`
- optional workspace/project/repo/branch context (branch requires repo)
- optional unverified source label
- content hash (dedup key)
- monotonically increasing version
- UTC `created_at` and `updated_at`
- nullable `deleted_at` (recoverable deletion)
- nullable `superseded_by` (ID of the memory that replaced it)

A memory is in exactly one state:

```text
active      deleted_at IS NULL AND superseded_by IS NULL
superseded  superseded_by IS NOT NULL
deleted     deleted_at IS NOT NULL
```

Only active memories are searched, listed by default, updated, or deduplicated against.

## Operations

```text
put        create; idempotent on identical content+context; may supersede others
get        read any state by ID
update     correct an active memory; requires expected_version
delete     soft-delete; revives memories it superseded
restore    undo delete
search     ranked lexical retrieval
list       enumerate by context/type/time, with cursor
contexts   enumerate known workspace/project/repo values
purge      CLI only: irreversible removal
```

## Retrieval

Candidate sources, all over active memories:

1. exact full-query substring (trigram index)
2. word FTS5/BM25 with Porter stemming after stopword removal and alias expansion
3. Chinese 3-gram matching (trigram index)
4. last resorts while results are short: word-part substrings (deploy → deployment) and separated Han bigrams

Ordering: context tier first (compatible before conflicting), then source order, then within-source rank (exact: newest first; FTS: BM25).

No public score or explanation object.

## Persistence

- WAL, busy timeout, bounded retry
- `BEGIN IMMEDIATE` for writes
- SQLite backup API
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
- push notifications / subscriptions (agents poll `list(updated_since=...)`)
- distributed storage
