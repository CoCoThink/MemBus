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

## Status

MemBus is at the initial architecture and V1 implementation stage. The first milestone is a reliable, inspectable local memory core before any semantic/vector layer is introduced.

See:

- [Requirements](docs/requirements.md)
- [Architecture](docs/architecture.md)
- [V1 technical specification](docs/v1-technical-spec.md)
- [Open-source design review](docs/open-source-review.md)
