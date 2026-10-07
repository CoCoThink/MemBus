# MemBus V1 Technical Specification

Status: implementation baseline  
Compatibility intent: V1 public service contracts should survive later retrieval-engine upgrades.

## 1. Technology choices

V1:

- Python 3.11+
- SQLite 3 with FTS5
- Python standard-library `sqlite3`
- UTC timestamps
- JSON only for audit/debug payloads, not for primary queryable fields

Optional adapter dependencies are isolated from the core package.

## 2. Package boundaries

```text
src/membus/
├── db.py                  # connections, transactions, backup, migrations
├── models.py              # domain dataclasses/enums
├── repository.py          # SQL persistence only
├── schema.sql             # V1 schema
├── retrieval/
│   ├── lexical.py         # query tokenization/FTS query building
│   ├── ranking.py         # transparent score composition
│   └── scope.py           # scope validation/affinity
└── services/
    └── memory_service.py  # stable domain API
```

Protocol adapters are added separately and must call the service layer.

## 3. Canonical schema

### 3.1 Schema metadata

```sql
CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO schema_meta(key, value)
VALUES ('schema_version', '1');
```

### 3.2 Memories

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

    status TEXT NOT NULL DEFAULT 'active',

    importance REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 1.0,
    trust REAL NOT NULL DEFAULT 0.5,

    source_type TEXT,
    source_ref TEXT,

    valid_from TEXT,
    valid_until TEXT,
    supersedes_id TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,

    CHECK (importance >= 0.0 AND importance <= 1.0),
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CHECK (trust >= 0.0 AND trust <= 1.0),

    FOREIGN KEY (supersedes_id) REFERENCES memories(id)
);
```

Supported V1 `memory_type` values are validated by the service:

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

```text
global
workspace
project
repo
branch
```

Supported V1 statuses:

```text
active
deleted
```

Do not encode type/scope/status as database enums that block forward-compatible additions. Validation belongs in the domain layer.

### 3.3 Aliases

```sql
CREATE TABLE aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

Aliases are normalized to lowercase before insertion.

### 3.4 Mutation event ledger

```sql
CREATE TABLE memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT,
    actor TEXT,
    created_at TEXT NOT NULL,

    FOREIGN KEY(memory_id) REFERENCES memories(id)
);
```

V1 event types:

```text
created
updated
deleted
```

The event ledger is append-only through normal application APIs.

### 3.5 Retrieval logs

```sql
CREATE TABLE retrieval_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT,
    scope_json TEXT,
    candidate_count INTEGER NOT NULL,
    result_ids_json TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    debug_json TEXT,
    created_at TEXT NOT NULL
);
```

A deployment may disable retrieval logging or redact query text.

### 3.6 FTS5

```sql
CREATE VIRTUAL TABLE memory_fts USING fts5(
    memory_id UNINDEXED,
    title,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);
```

FTS synchronization is performed by triggers:

```sql
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts(memory_id, title, content)
    VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER memories_au AFTER UPDATE OF title, content ON memories BEGIN
    DELETE FROM memory_fts WHERE memory_id = old.id;
    INSERT INTO memory_fts(memory_id, title, content)
    VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
    DELETE FROM memory_fts WHERE memory_id = old.id;
END;
```

Logical deletion leaves the FTS row present, but search joins against `memories.status='active'`. This makes restore semantics possible later without rebuilding FTS.

## 4. Indexes

```sql
CREATE INDEX idx_memories_status ON memories(status);
CREATE INDEX idx_memories_scope ON memories(scope);
CREATE INDEX idx_memories_workspace ON memories(workspace);
CREATE INDEX idx_memories_project ON memories(project);
CREATE INDEX idx_memories_repo ON memories(repo);
CREATE INDEX idx_memories_branch ON memories(branch);
CREATE INDEX idx_memories_updated_at ON memories(updated_at);
CREATE INDEX idx_events_memory_id ON memory_events(memory_id);
CREATE INDEX idx_retrieval_logs_created_at ON retrieval_logs(created_at);
```

Indexes may evolve without changing the canonical service contract.

## 5. Connection configuration

Every connection initializes:

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;
```

Notes:

- `WAL` enables concurrent readers with a writer.
- `foreign_keys` is connection-local and must be enabled on every connection.
- `busy_timeout` handles short lock contention.
- application code additionally uses bounded retry for `SQLITE_BUSY`/locked errors.

## 6. Transaction semantics

### 6.1 Read-only operations

Use an ordinary connection without manually opening a write transaction.

### 6.2 Application writes

All application-level write transactions use:

```sql
BEGIN IMMEDIATE;
```

This is an empirically verified requirement, not only a precaution. A
multi-process stress test showed that deferred `BEGIN` can allow several
processes to enter transactions and then fail immediately while upgrading to a
write lock at the first `INSERT`, even with a busy timeout configured.

SQLite permits only one writer regardless, so acquiring the writer reservation
at transaction start does not remove useful write parallelism. Instead it moves
contention to a point where `busy_timeout` and bounded retry can queue writers
predictably.

### 6.3 Read-modify-write

Read-modify-write operations also use `BEGIN IMMEDIATE`, for the same reason,
and retain the writer reservation while inspecting and mutating state.

Examples:

- create plus event-ledger append
- update with old-value audit payload
- logical delete with idempotency check
- alias mutation
- retrieval-log append
- future compare-and-supersede

### 6.4 Event atomicity

A mutation and its `memory_events` row MUST commit in the same transaction.

A state change without a corresponding event is a failed transaction.

## 7. Domain validation

### 7.1 Scope requirements

`global`:

- no context field is required

`workspace`:

- `workspace` required

`project`:

- `project` required
- workspace optional

`repo`:

- `repo` required
- project/workspace optional

`branch`:

- `repo` and `branch` required

The service rejects structurally invalid scopes before persistence.

### 7.2 Content requirements

- `content` must contain non-whitespace text
- `title` may be empty
- source fields may be empty
- all scores must be within `[0, 1]`

## 8. IDs and timestamps

Memory IDs are generated by the core, not by SQLite.

V1 format:

```text
mem_<uuid4-hex>
```

Example:

```text
mem_6cae16f7af7b41ad91e8df6ab0d479f9
```

This avoids coupling to integer row IDs and makes later import/export easier.

Timestamps are UTC ISO-8601 with timezone:

```text
2026-10-07T15:42:10.123456+00:00
```

## 9. Retrieval request contract

Internal request model:

```json
{
  "query": "postgres connection pool",
  "context": {
    "workspace": "work",
    "project": "payment",
    "repo": "payment-api",
    "branch": "main"
  },
  "types": ["incident", "decision", "procedure"],
  "limit": 10,
  "explain": true,
  "include_deleted": false
}
```

`types` is optional.

## 10. Query normalization

V1 performs deterministic normalization only:

1. trim whitespace
2. Unicode-aware lowercase where applicable
3. extract lexical tokens
4. expand exact aliases
5. preserve the original query for exact substring scoring

No remote model call is allowed in the baseline path.

### 10.1 FTS query construction

User text must not be passed to `MATCH` as raw FTS syntax.

The lexical module extracts safe tokens and builds an OR query such as:

```text
"postgres" OR "connection" OR "pool"
```

This prioritizes recall. Ranking determines final order.

If tokenization produces no usable terms, FTS is skipped and exact/substring search may still run.

## 11. Candidate retrieval

V1 uses two lexical sources:

### 11.1 Exact/substring candidates

A bounded query checks whether the full normalized query appears in title/content. This is valuable for:

- issue IDs
- file paths
- symbols
- error codes

### 11.2 FTS candidates

FTS5 retrieves a larger candidate set with BM25.

Candidate IDs are unioned before ranking.

This is intentionally different from vector-first architectures: exact lexical evidence can independently enter the candidate set.

## 12. Lexical score normalization

SQLite's `bm25()` returns lower values for better matches and can be negative depending on implementation details.

The repository exposes the raw BM25 value. The ranking layer converts it to a bounded positive signal.

V1 may use a monotonic transform such as:

```text
lexical = 1 / (1 + max(0, shifted_bm25))
```

or rank-normalized scoring.

The exact transform is internal and may change; the public contract is only that higher `lexical` means better.

Exact full-query substring match receives an additional bounded lexical boost, not a separate hidden filter.

## 13. Scope affinity

Given request context:

```yaml
workspace: work
project: payment
repo: payment-api
branch: feature-x
```

suggested V1 affinity:

| Memory scope | Applicable condition | Score |
|---|---|---:|
| global | always | 0.40 |
| workspace | workspace matches | 0.60 |
| project | project matches | 0.75 |
| repo | repo matches | 0.90 |
| branch | repo + branch match | 1.00 |

A scoped record that conflicts with the known required field is ineligible, not merely lower ranked.

Example: a `repo` memory for `other-api` is excluded when request repo is `payment-api`.

## 14. Recency score

V1 default uses exponential decay based on `updated_at`:

```text
recency = 0.5 ** (age_days / half_life_days)
```

Initial half-life:

```text
30 days
```

A floor may be used so old canonical knowledge does not become zero-value.

Recency is a weak signal, never a validity mechanism. Temporal validity belongs to a later lifecycle stage.

## 15. Final ranking

Initial default:

```text
score =
    0.60 * lexical
  + 0.15 * scope
  + 0.10 * recency
  + 0.10 * importance
  + 0.05 * trust
```

`confidence` is stored but is not part of the initial ranking formula; this avoids double-counting uncertain semantics before its meaning is calibrated.

When `explain=true`, return:

```json
{
  "score": 0.91,
  "signals": {
    "lexical": 0.95,
    "scope": 1.0,
    "recency": 0.72,
    "importance": 0.8,
    "trust": 1.0,
    "exact_match": true
  }
}
```

## 16. Multi-query search

Internal contract:

```json
{
  "queries": [
    "postgres connection pool",
    "max_connections",
    "worker concurrency"
  ],
  "context": {
    "project": "payment",
    "repo": "payment-api"
  },
  "limit": 10
}
```

V1 behavior:

1. execute each lexical query
2. union candidates by memory ID
3. retain the best per-query score for each candidate
4. optionally reward candidates appearing in multiple query result sets
5. apply final Top-K

The adapter makes one call; the backend may execute subqueries sequentially in V1 and optimize later.

## 17. Service API

Python-level stable service shape:

```python
MemoryService.put(...)
MemoryService.get(memory_id)
MemoryService.update(memory_id, ...)
MemoryService.delete(memory_id, ...)
MemoryService.search(request)
MemoryService.search_many(request)
```

Adapters must not import repository internals.

## 18. MCP V1 tools

### memory_put

Input:

```json
{
  "title": "Do not edit generated client",
  "content": "generated/client.ts is generated from OpenAPI.",
  "memory_type": "procedure",
  "scope": "repo",
  "workspace": "work",
  "project": "payment",
  "repo": "payment-api",
  "importance": 0.8,
  "trust": 0.9,
  "source_type": "user_rule",
  "source_ref": "conversation"
}
```

Output includes the canonical memory object.

### memory_get

```json
{ "id": "mem_..." }
```

### memory_update

Allows replacement of mutable fields. Scope changes are validated as a new complete scope.

### memory_delete

Logical delete by default.

### memory_search

Uses the request contract in section 9.

### memory_search_many

Uses the batch contract in section 16.

## 19. Backup API

Core function:

```python
backup_database(source_path, destination_path)
```

uses `sqlite3.Connection.backup()`.

Backup must work while the source is in WAL mode.

## 20. Schema initialization and migration

On startup:

1. open database
2. configure pragmas
3. if no schema exists, execute V1 schema
4. read `schema_version`
5. if version > supported: fail clearly
6. if version < supported: apply ordered migrations
7. run a lightweight integrity/schema check

V1 ships only migration `0 -> 1`, but the mechanism exists from day one.

## 21. Logging and privacy

Retrieval logging defaults should be configurable.

Modes:

```text
off
metadata-only
full
```

`metadata-only` records latency/result IDs without raw query text.

Mutation events SHOULD avoid copying entire sensitive content when a compact change summary is enough.

## 22. Test plan

### Storage

- initialize fresh DB
- reopen existing DB
- foreign keys enabled
- WAL enabled
- create/read/update/delete
- event written atomically
- FTS updated after insert/update
- logical delete excluded from search
- backup restores correctly

### Scope

- global applies everywhere
- project mismatch excluded
- repo mismatch excluded
- branch mismatch excluded
- malformed scoped writes rejected

### Retrieval

- exact issue ID ranks highly
- symbol/path queries produce candidates
- multi-word query produces FTS candidates
- aliases expand deterministically
- score explanation sums consistently
- type filter works
- search limit enforced

### Concurrency

- concurrent readers succeed
- short writer contention retries
- read-modify-write uses immediate transaction

### Compatibility

- adapter tests mock only the service API
- repository schema details are not exposed by adapter contracts

## 23. Golden retrieval set

Before adding embeddings, the repository should contain an evaluation fixture with approximately 50–100 representative queries.

Each case records:

```yaml
query: "why can settlement not run concurrently"
context:
  project: payment
expected:
  - mem-settlement-idempotency
```

Metrics:

- Recall@5
- Recall@10
- MRR
- Precision@5
- zero-result rate
- P50/P95 latency
- wrong-scope hit rate

No vector subsystem should be merged merely because it is available; it should demonstrate measurable benefit against this set.

## 24. V1 release gate

V1 can be tagged when:

- all acceptance criteria in `requirements.md` pass
- schema and service contracts are documented
- no LLM/network dependency is required
- backup/restore is tested
- baseline retrieval metrics are recorded
- at least one MCP client can use the adapter without directly knowing SQLite
