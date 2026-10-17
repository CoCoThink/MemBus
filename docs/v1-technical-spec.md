# MemBus V1 Technical Specification

Status: development baseline

## Runtime package

```text
src/membus/
├── core.py
├── cli.py
├── doctor.py
└── adapters/
    └── mcp_server.py
```

`tools/evaluate.py` is repository/CI tooling, not runtime API.

## Canonical schema

The schema is inlined in `core.py`.

```sql
CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    workspace TEXT,
    project TEXT,
    repo TEXT,
    branch TEXT
);

CREATE TABLE aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);
```

FTS5 stores `memory_id/title/content`; triggers synchronize insert, title/content update, and delete.

## Context

There is no scope enum or column.

Stored/query context consists of:

```text
workspace
project
repo
branch
```

Branch context requires repo context.

A stored row is eligible only when each non-empty stored context dimension is present and equal in the query (`memory_context ⊆ query_context`). Missing query context therefore excludes more-specific memories.

## Transactions

Runtime policy is intentionally fixed:

```text
WAL
busy_timeout = 5000 ms
synchronous = NORMAL
BEGIN IMMEDIATE for writes
5 bounded retry attempts
```

These settings are not public constructor configuration in V1.

## Retrieval

```text
query
  ├─ exact substring
  └─ tokens -> aliases -> FTS5
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

Search returns `list[Memory]`. No request/result wrapper, score, or explain mode exists.

## Public Python API

```python
bus = MemBus(path)

bus.put(...)
bus.get(...)
bus.update(...)
bus.delete(...)
bus.search(...)
bus.put_alias(...)
bus.backup(...)
```

`Memory` is the only record type exposed.

## MCP contract

```text
memory_put
memory_get
memory_update
memory_delete
memory_search
```

## Evaluation

Golden evaluation lives outside the runtime package:

```bash
python tools/evaluate.py evals/golden_v1.json
```

New retrieval machinery must demonstrate measurable benefit before entering the runtime.
