# MemBus

**MemBus** is a user-owned, tool-agnostic memory store for AI agents.

Claude Code, Codex, Cursor, ChatGPT, and other clients can share one local canonical store through MCP or CLI without making any agent vendor the system of record.

## Design principles

- **User-owned data** — agents are clients, not memory owners.
- **Local-first** — SQLite is the canonical store.
- **Lexical-first** — exact matching plus FTS5/BM25 is the baseline.
- **Context isolation** — workspace/project/repo/branch mismatches are excluded.
- **Derived search index** — FTS can be rebuilt from canonical rows.
- **Progressive complexity** — add new subsystems only after measured failures.

## Current core

```text
MCP / CLI
    │
MemoryService
    │
SQLiteStore
    │
    ├── memories
    ├── aliases
    └── memory_fts
```

The canonical memory row contains:

```text
id
title
content
memory_type
workspace
project
repo
branch
source_type
source_ref
created_at
updated_at
```

There is no explicit scope field. Context specificity is inferred from which context fields are populated.

There is no soft-delete state. `memory_delete` physically deletes the canonical row and the FTS trigger removes the derived index row.

## MCP tools

The stdio MCP adapter exposes five tools:

```text
memory_put
memory_get
memory_update
memory_delete
memory_search
```

Local agents and ChatGPT through Secure MCP Tunnel use the same tool surface.

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
  --project payment \
  --repo payment-api

membus --db ./memory.db backup ./backup.db
```

Run the MCP server:

```bash
MEMBUS_DB=./memory.db membus-mcp
```

## Retrieval

```text
query
  ├── exact full-query substring
  └── tokens -> aliases -> FTS5/BM25
           │
           ▼
      context filter
           │
           ▼
       exact first
       then FTS
           │
           ▼
          Top-K
```

Search results contain lightweight match metadata:

```json
{
  "memory": {"id": "mem_..."},
  "match": "exact"
}
```

There is no synthetic numeric relevance score.

## Reliability

Implemented and tested:

- SQLite WAL
- busy timeout and bounded busy retry
- `BEGIN IMMEDIATE` for application writes
- multi-process writer stress test
- SQLite backup API
- FTS synchronization triggers
- `membus doctor`
- isolated Golden Retrieval Evaluation
- Python 3.11 / 3.12 CI
- MCP round-trip tests

Run retrieval evaluation:

```bash
membus eval evals/golden_v1.json \
  --min-recall-5 0.95 \
  --min-recall-10 0.95 \
  --max-wrong-context-rate 0.0
```

Still deliberately absent:

- schema migration compatibility
- mutation history
- runtime retrieval logging
- soft-delete/restore
- embeddings/vector database
- graph database
- LLM query rewriting/reranking
- automatic memory extraction/consolidation
- ACL/multi-user tenancy
- distributed storage

See:

- [Requirements](docs/requirements.md)
- [Architecture](docs/architecture.md)
- [V1 technical specification](docs/v1-technical-spec.md)
- [Retrieval evaluation](docs/evaluation.md)
- [ChatGPT Secure MCP Tunnel integration](docs/chatgpt-secure-tunnel.md)
- [Secure MCP Tunnel deployment runbook](docs/secure-tunnel-deployment.md)
