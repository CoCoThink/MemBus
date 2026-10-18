# MemBus

**MemBus** is a user-owned local memory bus for AI agents.

Claude Code, Codex, Cursor, ChatGPT, and other MCP clients share one SQLite store: each agent can load what applies to its current context, see what others recorded, and keep shared memory current without duplicating or silently overwriting it. No agent vendor is the system of record.

> **Status:** V1 is implemented. Design rationale and measured trade-offs are in [Architecture → Design decisions](docs/architecture.md#design-decisions).

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
 ├─ aliases
 ├─ memory_fts      (words, Porter stemming)
 └─ memory_trigram  (substrings, Chinese)
```

A memory has a title, content (≤ 4000 chars), a type (`fact | decision | preference | procedure | incident | note`), optional `workspace / project / repo / branch` context, an optional `source` label, a `version`, UTC timestamps, and a lifecycle state:

```text
active ──update (expected_version)──► active
active ──put(supersedes=[id])──────► superseded   (the world changed)
active ──delete────────────────────► deleted ──restore──► active
```

Identical content in the same context is stored once: `put` is idempotent and also returns `similar` near-matches so the agent can update or supersede instead of duplicating.

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
 compatible context first, conflicting as fallback (strict=True: compatible only)
          │
          ▼
        Top-K over active memories
```

`list` enumerates memories that apply to a context, newest first, with `updated_since` for "what changed since my last session". All lexical; no LLM, embedding model, or hosted service.

## MCP tools

```text
memory_search     memory_get       memory_delete
memory_list       memory_put       memory_restore
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

membus --db ./memory.db list --project payment --repo payment-api
membus --db ./memory.db backup ./backup.db
```

Run MCP:

```bash
MEMBUS_DB=./memory.db membus-mcp
```

## Reliability

- SQLite WAL, busy timeout, bounded retry, `BEGIN IMMEDIATE`
- optimistic concurrency (`expected_version`) for updates
- content-hash unique index for idempotent writes
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
- [V1 technical specification](docs/v1-technical-spec.md)
- [Agent protocol](docs/agent-protocol.md)
- [Retrieval evaluation](docs/evaluation.md)
- [ChatGPT Secure MCP Tunnel integration](docs/chatgpt-secure-tunnel.md)
- [Secure MCP Tunnel deployment runbook](docs/secure-tunnel-deployment.md)
