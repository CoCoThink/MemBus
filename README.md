# MemBus

**MemBus** is a user-owned local memory store for AI agents.

Claude Code, Codex, Cursor, ChatGPT, and other MCP clients can share one SQLite store without making any agent vendor the system of record.

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
 └─ FTS5
```

Canonical memory stores the following fields:

```text
id
title
content
memory_type
workspace
project
repo
branch
source
created_at
updated_at
deleted_at
```

Timestamps use UTC ISO-8601. `source` is an optional user-supplied label, not a verified identity. Deletion is recoverable and excluded from normal reads/search, with no edit revision history.

## Retrieval

```text
query
  ├─ exact full-query substring
  └─ tokens -> aliases -> FTS5/BM25
          │
          ▼
      context filter
          │
          ▼
 exact first, then FTS
          │
          ▼
        Top-K
```

Python and CLI searches default to broad context preference (`strict=False`): conflicting memories can be fallback candidates. Set `strict=True` (or CLI `--strict`) to return only applicable memories, rejecting conflicts and more-specific memories where query context is missing. MCP search defaults to `strict=True` for agent safety; opt out with `strict=False` for exploration. Strict mode does not implement access control.

## MCP tools

```text
memory_put
memory_get
memory_update
memory_delete
memory_restore
memory_search
```

Local agents and ChatGPT through Secure MCP Tunnel use the same six tools.

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

# --strict filters conflicting or narrower context memories.
# Delete is recoverable: membus --db ./memory.db restore MEM_ID
membus --db ./memory.db backup ./backup.db
```

Run MCP:

```bash
MEMBUS_DB=./memory.db membus-mcp
```

## Reliability

The small core still keeps the engineering controls that have demonstrated value:

- SQLite WAL
- busy timeout and bounded busy retry
- `BEGIN IMMEDIATE` for writes
- multi-process writer stress coverage
- FTS synchronization triggers
- SQLite backup API
- `membus doctor`
- Python 3.11 / 3.12 CI
- MCP round-trip tests
- isolated Golden Retrieval Evaluation

Run the repository evaluation tool:

```bash
python tools/evaluate.py evals/golden_v1.json \
  --min-recall-5 0.95 \
  --min-recall-10 0.95 \
  --max-context-preference-error-rate 0.0
```

Deliberately absent until real failures justify them:

- schema compatibility migrations (pre-deployment databases should be recreated)
- edit revision history
- runtime retrieval logging
- batch/search-many API
- embeddings/vector DB
- graph DB
- LLM query rewrite/rerank
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
