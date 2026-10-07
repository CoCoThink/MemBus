# MemBus

**MemBus** is a user-sovereign, tool-agnostic memory bus for AI agents.

Its purpose is to keep long-term knowledge and memory under the user's control, while allowing disposable clients such as Claude Code, Codex, Cursor, Kimi, Grok, and future agents to retrieve and write authorized memory through stable adapters.

## Design principles

- **User-owned canonical memory** — agent vendors are clients, not the system of record.
- **Local-first** — the V1 canonical store is SQLite.
- **Lexical-first retrieval** — SQLite FTS5/BM25 is the baseline; embeddings are optional derived indexes introduced only when evaluation proves they are necessary.
- **Indexes are disposable** — FTS, vector, graph, and caches must be rebuildable from canonical data.
- **Read and write paths are separated** — retrieval and memory formation evolve independently.
- **Scope is a boundary, not a tag** — workspace/project/repo/branch context must prevent cross-project memory pollution.
- **Explainable retrieval** — every returned memory should be traceable to retrieval signals and provenance.
- **Progressive complexity** — every new subsystem must justify itself with measured retrieval or operational failures.

## Initial V1 scope

V1 intentionally stays small:

- SQLite canonical store
- WAL mode and explicit transaction boundaries
- FTS5 full-text retrieval
- memory scope and type metadata
- provenance and event ledger
- retrieval logging
- six core memory operations:
  - search
  - search-many
  - get
  - put
  - update
  - delete
- MCP adapter as an outer layer, not as the core architecture
- no mandatory embedding model
- no vector database
- no autonomous memory extraction
- no graph database

## Repository layout

```text
MemBus/
├── docs/
│   ├── requirements.md
│   ├── architecture.md
│   ├── v1-technical-spec.md
│   └── open-source-review.md
├── src/membus/
│   ├── db.py
│   ├── models.py
│   ├── repository.py
│   ├── schema.sql
│   ├── retrieval/
│   └── services/
├── tests/
├── pyproject.toml
└── README.md
```

## Quick start

Core only:

```bash
python -m pip install -e .
```

With the MCP adapter:

```bash
python -m pip install -e '.[mcp]'
```

Run the local stdio MCP server:

```bash
membus-mcp
```

By default the canonical database is:

```text
~/.membus/memory.db
```

Override it with:

```bash
MEMBUS_DB=/path/to/memory.db membus-mcp
```

Retrieval logging is off by default so reads do not become writes. For development/auditing:

```bash
MEMBUS_RETRIEVAL_LOG_MODE=metadata-only membus-mcp
# or
MEMBUS_RETRIEVAL_LOG_MODE=full membus-mcp
```

The current MCP v2 adapter exposes exactly six tools:

```text
memory_put
memory_get
memory_update
memory_delete
memory_search
memory_search_many
```

## Current implementation status

Implemented baseline:

- SQLite canonical store
- WAL + busy timeout
- explicit transactions and `BEGIN IMMEDIATE` for read-modify-write paths
- consistent SQLite backup API
- FTS5/BM25 lexical candidates
- exact identifier/substring candidate path
- deterministic aliases
- workspace/project/repo/branch scope isolation
- explainable ranking
- mutation event ledger
- optional retrieval logs
- logical deletion
- multi-query fusion
- MCP v2 stdio adapter
- GitHub Actions on Python 3.11 and 3.12
- in-memory MCP round-trip tests

Still deliberately absent:

- embeddings/vector database
- LLM query rewrite/reranking
- automatic memory extraction
- autonomous consolidation
- graph database
- distributed storage

These will only be introduced against measured retrieval or operational failures.

See:

- [Requirements](docs/requirements.md)
- [Architecture](docs/architecture.md)
- [V1 technical specification](docs/v1-technical-spec.md)
- [Open-source design review](docs/open-source-review.md)
