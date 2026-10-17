# MemBus V1 Technical Specification

Status: development baseline

## 1. Technology

- Python 3.11+
- SQLite + FTS5
- standard-library `sqlite3`
- optional MCP dependency

## 2. Package shape

```text
src/membus/
├── store.py
├── models.py
├── schema.sql
├── doctor.py
├── evaluation.py
├── retrieval/
│   └── lexical.py
├── services/
│   └── memory_service.py
└── adapters/
    └── mcp_server.py
```

## 3. Schema

```sql
CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    workspace TEXT,
    project TEXT,
    repo TEXT,
    branch TEXT,
    source_type TEXT,
    source_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);

CREATE VIRTUAL TABLE memory_fts USING fts5(
    memory_id UNINDEXED,
    title,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);
```

FTS triggers synchronize insert, title/content update, and delete.

## 4. Context

There is no scope enum or column.

Specificity is inferred from populated context:

```text
workspace
project
repo
branch
```

Stored and query branch context requires repo context.

Search excludes a row only when the row and query both supply the same dimension and values differ.

## 5. Transactions

Connections use WAL, busy timeout, and `synchronous=NORMAL`.

Every application mutation uses:

```sql
BEGIN IMMEDIATE;
```

with bounded retry for busy/locked errors.

## 6. Retrieval

```text
query
  ├── exact substring
  └── safe tokens -> aliases -> FTS5
          │
          ▼
      SQL context filter
          │
          ▼
 exact first, then BM25
          │
          ▼
        Top-K
```

Search returns:

```json
{
  "memory": {"id": "mem_..."},
  "match": "exact"
}
```

or `"match": "fts"`.

No numeric relevance score or explain mode exists.

## 7. Writes

- `put` inserts a row
- `get` fetches by ID
- `update` validates and replaces mutable fields
- `delete` physically deletes the row
- FTS is updated by triggers

## 8. MCP contract

```text
memory_put
memory_get
memory_update
memory_delete
memory_search
```

## 9. Backup and doctor

Backup uses `sqlite3.Connection.backup()`.

Doctor checks initialization, WAL, FTS5, integrity, FTS consistency, and write readiness.

## 10. Evaluation

Golden evaluation tracks Recall@5/10, MRR, zero-result rate, wrong-context rate, and P50/P95 latency.

New retrieval machinery must demonstrate measurable benefit before being added.
