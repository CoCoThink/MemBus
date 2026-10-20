# MemBus

**MemBus** is a user-owned local memory bus for AI agents.

Claude Code, Codex, Cursor, ChatGPT, and other MCP clients share one SQLite store: each agent can load what applies to its current context, see what others recorded, and keep shared memory current without duplicating or silently overwriting it. No agent vendor is the system of record.

> **Status:** Implemented with schema 2. Design rationale and measured trade-offs are in [Architecture → Design decisions](docs/architecture.md#design-decisions).

## Core

```text
MCP / CLI
    │
    ▼
  MemBus
    │
    ▼
 SQLite
 ├─ memories
 ├─ metadata    (global revision counter, sync generation)
 ├─ aliases
 ├─ memory_fts      (words, Porter stemming)
 └─ memory_trigram  (substrings, Chinese)
```

A memory has a title, content (≤ 4000 chars), a type (`fact | decision | preference | procedure | incident | note`), optional `workspace / project / repo / branch` context, an optional `source` label, a globally allocated `revision`, UTC timestamps, and a lifecycle state:

```text
active ──update (expected_revision)──► active
active ──put(supersedes=[{id, expected_revision}])──► superseded
active ──delete────────────────────► deleted ──restore──► active
```

Identical content in the same context is stored once: `put` is idempotent and also returns `similar` near-matches so the agent can update or supersede instead of duplicating.

`update`, `delete`, `restore`, and every supersedes target require the revision from the last read. Deleting or purging a successor leaves older memories superseded; `restore` on such a predecessor makes it current again, as it does for a deleted memory. Superseding with the same replacement again succeeds without changing revisions.

## Retrieval

```text
query
  ├─ exact substring        (trigram index)
  ├─ words → stopwords out → aliases → FTS5/BM25 (Porter)
  ├─ Chinese 3-grams        (trigram index)
  └─ last resorts when results are short: word parts (deploy → deployment),
     Han bigrams
          │
          ▼
 no known context conflicts first; strict=True requires direct applicability
          │
          ▼
        Top-K over active memories
```

`list` discovers memories even if the original context is unknown: with no context it browses all active memories; supplied context fields match identical or global (NULL) values, while omitted fields are unconstrained. These are candidates, not necessarily rules for the current task. Agents must check stored scope before applying a memory. `updated_since` gives a best-effort "what changed among these memories since then". `sync` reads latest states globally in ascending revision order, including deleted and superseded memories. Timestamps are informational; clock drift cannot hide changes. All lexical; no LLM, embedding model, or hosted service.

For a cache, start `sync` at revision zero. Apply each result by ID and save `generation` and `next_revision` together. Continue while `has_more`, then reuse that pair for later polls. Apply context/type filters locally so scope changes can remove cached entries. Intermediate edits may be coalesced; this provides eventual state convergence. Purge changes the generation, requiring clients to discard their caches and restart.

## MCP tools

```text
memory_search     memory_get       memory_delete
memory_list       memory_put       memory_restore
memory_sync
memory_contexts   memory_update
```

Tool descriptions and server instructions follow the [agent protocol](docs/agent-protocol.md). Local agents and ChatGPT through Secure MCP Tunnel use the same tools; every connected client is trusted with full access.

## Quick start

```bash
python -m pip install -e '.[mcp]'

membus --db ./memory.db doctor

membus --db ./memory.db put \
  --title "Do not edit generated client" \
  --content "generated/client.ts is generated from OpenAPI." \
  --memory-type procedure \
  --project payment \
  --repo payment-api

membus --db ./memory.db search "generated/client.ts" \
  --project payment --repo payment-api

membus --db ./memory.db list                   # browse all active memories
membus --db ./memory.db list --project payment  # partial context finds repo memories
membus --db ./memory.db sync
membus --db ./memory.db sync --after-revision 42 --generation GENERATION_FROM_LAST_PAGE
membus --db ./memory.db backup ./backup.db
```

Run MCP over stdio (the default):

```bash
MEMBUS_DB=./memory.db membus-mcp
```

Or start a shared Streamable HTTP server:

```bash
membus-mcp --transport streamable-http --host 127.0.0.1 --port 8301 \
  --db ./memory.db
```

Connect MCP clients to `http://127.0.0.1:8301/mcp`. Startup options also accept
environment variables for service deployments; see [MCP HTTP deployment](docs/mcp-http.md).

## Reliability

- SQLite WAL, busy timeout, bounded retry, `BEGIN IMMEDIATE`
- global revisions allocated inside write transactions for sync and optimistic concurrency
- content-hash unique index and state-based supersedes retries
- generation-bound sync positions, invalidated by purge
- FTS synchronization triggers
- SQLite backup API, `membus doctor`, CLI-only `purge`
- multi-process writer stress tests, MCP round-trip tests
- isolated Golden Retrieval Evaluation with a 100k-memory latency run

```bash
python tools/evaluate.py evals/golden_v1.json \
  --min-recall-5 0.95 \
  --min-recall-10 0.95 \
  --max-context-preference-error-rate 0.0 \
  --max-superseded-leak-rate 0.0
```

Deliberately absent until real failures justify them:

- revision history
- per-client permissions / ACL / multi-user tenancy
- runtime retrieval logging, batch search
- embeddings/vector DB, graph DB
- LLM query rewrite/rerank
- automatic memory extraction/consolidation
- push subscriptions, distributed storage

See:

- [Requirements](docs/requirements.md)
- [Architecture](docs/architecture.md)
- [Technical specification](docs/technical-spec.md)
- [Agent protocol](docs/agent-protocol.md)
- [Retrieval evaluation](docs/evaluation.md)
- [ChatGPT Secure MCP Tunnel integration](docs/chatgpt-secure-tunnel.md)
- [Secure MCP Tunnel deployment runbook](docs/secure-tunnel-deployment.md)
