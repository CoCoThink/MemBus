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
    branch TEXT,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);
```

FTS5 stores live `memory_id/title/content`; triggers synchronize insert, title/content update, soft-delete and restore. UTC timestamps are stored as ISO-8601 text.

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

Broad mode (`strict=False`) uses preference tiers with fallback. Strict mode (`strict=True`) excludes conflicting and narrower-than-query memories before top-K candidate limits. Python, CLI, and MCP all default to broad; strict filtering is opt-in. Neither implements authentication or authorization.

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
   context preference tiers
          │
          ▼
preferred context first
fallback context second
          │
          ▼
 exact first within tier
 then BM25
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
bus.delete(...)    # soft-delete
bus.restore(...)
bus.search(..., strict=True)
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
memory_restore
memory_search
```

## Evaluation

Golden evaluation lives outside the runtime package:

```bash
python tools/evaluate.py evals/golden_v1.json
```

New retrieval machinery must demonstrate measurable benefit before entering the runtime.
