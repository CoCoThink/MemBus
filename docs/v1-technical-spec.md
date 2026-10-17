# MemBus V1 Technical Specification

Status: development baseline

## 1. Technology

- Python 3.11+
- SQLite + FTS5
- standard-library `sqlite3`
- UTC ISO-8601 timestamps
- optional MCP dependency outside the core

No network, model API, embedding library, or hosted service is required by the core.

## 2. Package layout

```text
src/membus/
├── db.py
├── models.py
├── repository.py
├── schema.sql
├── doctor.py
├── evaluation.py
├── retrieval/
│   ├── lexical.py
│   ├── ranking.py
│   └── scope.py
├── services/
│   └── memory_service.py
└── adapters/
    └── mcp_server.py
```

There is no migration package before first deployment.

## 3. Database schema

```sql
CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    workspace TEXT,
    project TEXT,
    repo TEXT,
    branch TEXT,
    source_type TEXT,
    source_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE memory_fts USING fts5(
    memory_id UNINDEXED,
    title,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);
```

Triggers synchronize `memory_fts` after canonical insert, title/content update, and physical delete.

A memory is active when:

```sql
deleted_at IS NULL
```

No event table, retrieval-log table, schema-version table, or speculative lifecycle/scoring columns exist.

## 4. Initialization

`Database.initialize()`:

1. opens/creates the SQLite file
2. enables WAL
3. executes the idempotent current `schema.sql`

During pre-deployment development, incompatible schema changes may require deleting/recreating local databases. Compatibility machinery is added only before a real compatibility commitment.

## 5. Transactions

Connections configure:

```text
busy_timeout = 5000 ms
synchronous = NORMAL
```

Read-only operations use ordinary connections.

Every application write uses:

```sql
BEGIN IMMEDIATE;
```

with bounded retry for transient busy/locked errors.

## 6. Domain validation

Supported memory types:

```text
fact
decision
preference
procedure
incident
failure
note
```

Supported scopes:

| scope | required context |
|---|---|
| global | none |
| workspace | workspace |
| project | project |
| repo | repo |
| branch | repo + branch |

`content` must contain non-whitespace text.

IDs are generated as:

```text
mem_<uuid4hex>
```

## 7. Retrieval

```text
query
  ├─ exact full-query substring
  └─ safe tokens -> aliases -> FTS5
          │
          ▼
     candidate union
          │
          ▼
     scope eligibility
          │
          ▼
     lexical + scope
          │
          ▼
        Top-K
```

Exact matches receive lexical score `1.0`.

FTS candidates are ordered by SQLite BM25 and converted to a bounded rank-based lexical score.

Final ranking:

```text
score =
    0.80 * lexical
  + 0.20 * scope
```

When `explain=true`, signals include lexical score, scope score/reason, exact-match state, raw BM25 where applicable, and alias expansion where applicable.

## 8. Scope affinity

```text
global     0.40
workspace  0.60
project    0.75
repo       0.90
branch     1.00
```

Known context mismatches are excluded rather than merely down-ranked.

## 9. Multi-query

`search_many`:

1. runs each query
2. unions results by memory ID
3. keeps the best score per memory
4. sorts once
5. returns Top-K

No model call or extra fusion bonus is used.

## 10. Write semantics

`put` creates a canonical row.

`update` validates the resulting full memory shape and updates mutable fields.

`delete` sets `deleted_at` and `updated_at`.

`get(..., include_deleted=True)` can inspect a logically deleted memory by known ID.

Search never returns deleted memories.

## 11. Service / MCP operations

```text
memory_put
memory_get
memory_update
memory_delete
memory_search
memory_search_many
```

Local clients and ChatGPT via Secure MCP Tunnel use the same tool surface.

## 12. Backup and doctor

Backups use `sqlite3.Connection.backup()`.

`membus doctor` checks:

- initialization
- WAL mode
- FTS5 MATCH
- SQLite integrity
- canonical/FTS consistency
- write transaction readiness

## 13. Evaluation

The isolated Golden Retrieval Evaluation tracks:

- Recall@5
- Recall@10
- MRR
- Precision@5
- zero-result rate
- wrong-scope hit rate
- P50/P95 latency

New retrieval complexity must improve measured failures before entering the default path.
