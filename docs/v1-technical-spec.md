# MemBus V1 Technical Specification

Status: implemented in `src/membus` (no compatibility with pre-deployment databases)

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

The schema is inlined in `core.py`. Requires SQLite ≥ 3.34 (FTS5 trigram tokenizer).

```sql
CREATE TABLE memories (
    rid INTEGER PRIMARY KEY,          -- stable FTS rowid; never exposed
    id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    workspace TEXT,
    project TEXT,
    repo TEXT,
    branch TEXT,
    source TEXT,
    content_hash TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    superseded_by TEXT                -- memory ID; no FK, delete/purge keep it clean
);

-- Idempotent put among active memories.
CREATE UNIQUE INDEX memories_active_hash ON memories(content_hash)
    WHERE deleted_at IS NULL AND superseded_by IS NULL;

-- list ordering and updated_since cursor.
CREATE INDEX memories_updated ON memories(updated_at, id);

-- `contexts` is a pure index scan.
CREATE INDEX memories_context ON memories(workspace, project, repo)
    WHERE deleted_at IS NULL AND superseded_by IS NULL;

-- Revival lookup when a successor is deleted or purged.
CREATE INDEX memories_successor ON memories(superseded_by)
    WHERE superseded_by IS NOT NULL;

CREATE TABLE aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);

-- Both FTS tables use memories.rid as rowid.
CREATE VIRTUAL TABLE memory_fts USING fts5(
    title, content,
    tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE memory_trigram USING fts5(
    title, content,
    tokenize = 'trigram case_sensitive 0'
);
```

`rid` is an explicit `INTEGER PRIMARY KEY` (not the implicit rowid) because `VACUUM` may renumber implicit rowids, and FTS rows are keyed by it. Joining and deleting by rowid keeps update/delete/restore sub-millisecond at 100k memories; an `UNINDEXED memory_id` column would make every FTS delete a full scan.

`PRAGMA user_version` holds the schema version (`1`). A database with tables but a different version is refused with an "incompatible schema; recreate" error; there is no migration layer. Initialization runs in one `BEGIN IMMEDIATE` transaction so concurrently starting processes cannot race.

Triggers keep both FTS tables equal to the set of active memories: insert indexes active rows; any update of `title`, `content`, `deleted_at`, or `superseded_by` deletes and re-inserts if still active; physical delete removes.

Timestamps are UTC ISO-8601 with microseconds and `Z`. `updated_at` and `version` change on every mutation, including delete, restore, and being superseded.

## Validation

- `content`: stripped, 1–4000 characters
- `title`: stripped, ≤ 200 characters
- `memory_type`: one of `fact, decision, preference, procedure, incident, note`
- context values: stripped, empty → NULL; `branch` requires `repo`
- `content_hash`: SHA-256 hex of the normalized (whitespace-collapsed, casefolded) tuple `memory_type, workspace, project, repo, branch, title, content`, joined with `\x1f`

## Public Python API

```python
bus = MemBus(path)

bus.put(*, content, memory_type, title="", workspace=None, project=None,
        repo=None, branch=None, source=None, supersedes=()) -> PutResult
bus.get(memory_id) -> Memory | None                     # any state
bus.update(memory_id, *, expected_version, title=..., content=...,
           memory_type=..., workspace=..., project=..., repo=...,
           branch=..., source=...) -> Memory
bus.delete(memory_id) -> bool
bus.restore(memory_id) -> bool
bus.search(query, *, workspace=None, project=None, repo=None, branch=None,
           types=(), limit=10, strict=False) -> list[Memory]
bus.list(*, workspace=None, project=None, repo=None, branch=None,
         types=(), updated_since=None, include_inactive=False,
         limit=50, cursor=None) -> ListPage
bus.contexts() -> list[ContextCount]
bus.purge(memory_id) -> bool
bus.put_alias(alias, canonical) -> None
bus.backup(destination) -> Path
```

Records:

```python
@dataclass(frozen=True)
class Memory:
    id; title; content; memory_type
    workspace; project; repo; branch; source
    version; created_at; updated_at; deleted_at; superseded_by
    # state is derived: "active" | "superseded" | "deleted"

@dataclass(frozen=True)
class PutResult:
    memory: Memory
    created: bool            # False: identical active memory already existed
    superseded: list[str]    # IDs marked superseded by this put
    similar: list[Memory]    # ≤ 3 compatible active near-matches, excluding memory

@dataclass(frozen=True)
class ListPage:
    memories: list[Memory]
    next_cursor: str | None

@dataclass(frozen=True)
class ContextCount:
    workspace; project; repo
    count: int               # active memories
```

`content_hash` is internal and not exposed.

Errors:

```text
ValueError            validation failure
NotFound(KeyError)    no memory / not in the required state
VersionConflict       update with stale expected_version; carries current Memory
DuplicateMemory       update would collide with another active memory; carries it
```

## Operation semantics

### put

In one `BEGIN IMMEDIATE` transaction:

1. If an active row has the same `content_hash`, return it with `created=False`. `supersedes` is still applied.
2. Otherwise insert with `version=1`.
3. For each ID in `supersedes`: it must be active and must not be the new memory, else `NotFound`/`ValueError` and the whole put rolls back. Set `superseded_by`, bump `version` and `updated_at`.

After commit, `similar` is computed from `search(first 500 chars of title + content, <same context>, strict=True, limit=10)`: drop the memory itself and the superseded IDs, then keep candidates whose lowercase word tokens (plus Han bigrams) share ≥ 2 tokens and ≥ 60% of the smaller token set with the new memory, at most 3. The overlap filter exists because an OR search always returns something; `similar` must mean "probably the same idea".

### update

Only active memories (`NotFound` otherwise). At least one field is required. Fields not passed (the `UNSET` sentinel) are unchanged; passing `None` clears `title`, a context field, or `source`; `content` and `memory_type` cannot be cleared. If `version != expected_version`, raise `VersionConflict(current)`. If the result equals the current row, return it unchanged (no version bump). If the new hash matches a different active row, raise `DuplicateMemory(existing)`. Otherwise write, `version += 1`.

At the MCP and CLI layers "omitted" is `null`/absent and **an empty string clears** (MCP `null` cannot mean both).

### delete / restore

`delete`: active or superseded → deleted; clear `superseded_by` on rows pointing to this ID (they become active again, `version += 1`). If a revived row's hash now collides with another active row, the revived row stays superseded by that row instead. Returns False if already deleted or missing.

`restore`: deleted → superseded if `superseded_by` is still set and its target is not deleted (the successor may itself be superseded); otherwise active with `superseded_by` cleared. If the hash collides with an active row, raise `DuplicateMemory`. Returns False if not deleted or missing.

### purge

Physical delete of any state; clears `superseded_by` references to it with the same revival rule as `delete`. CLI only.

### list

Returns memories compatible with the supplied context (same rule as strict search), filtered by `types` and `updated_at > updated_since`. `include_inactive=True` adds deleted and superseded rows so a sync reader sees removals. Ordered `updated_at DESC, id DESC`; `cursor` is an opaque encoding of the last `(updated_at, id)`. `limit` 1–200.

### contexts

Distinct `(workspace, project, repo)` over active memories with counts, ordered by count descending.

### search

Active memories only. `limit` 1–100. The query is normalized (casefold, whitespace-collapsed).

1. **Exact**: ≥ 3 characters: `memory_trigram MATCH "<phrase>"` (a trigram phrase match is a case-insensitive substring match over title or content). Han queries of 1–2 characters: `instr` scan (the only scan path). 1–2 ASCII characters: skipped; whole tokens are found by source 2. Ordered by `updated_at DESC`.
2. **Word**: Unicode tokens, English stopwords removed (kept if the query is all stopwords), at most 32 → alias expansion → quoted OR query over `memory_fts`. Ordered by `bm25(memory_fts)`.
3. **Chinese**: OR of up to 12 distinct Han 3-grams over `memory_trigram`, ordered by BM25.
4. **Word parts** (only while compatible results < `limit`): tokens of ≥ 5 characters (≤ 8, non-Han) as phrases over `memory_trigram`, ordered by BM25. Recovers derivations Porter misses, e.g. `deploy` → `deployment`.
5. **Han bigrams** (only while compatible results < `limit`): up to 12 Han 2-grams via `instr`; a memory needs ≥ 2 distinct bigram hits, e.g. `支付部署` → `支付服务…部署`.

Each source applies the type filter and (strict) context filter in SQL before LIMIT and orders compatible before conflicting. Merge: compatible tier from sources 1→5, then conflicting tier 1→5, deduplicated by ID, cut at `limit`.

Stopword removal is a latency measure, not a recall measure: at 100k memories it halves P95 (54 → 26 ms on the golden queries) with identical recall, because OR queries over words like `how`/`should` otherwise score enormous posting lists.

## Transactions

```text
WAL
busy_timeout = 5000 ms
synchronous = NORMAL
BEGIN IMMEDIATE for writes
5 bounded retry attempts with exponential backoff
```

Not public configuration.

## MCP contract

Tools (descriptions are normative; full text in [agent protocol](agent-protocol.md)):

```text
memory_search    query, workspace?, project?, repo?, branch?, types?, limit=10, strict=false
memory_list      workspace?, project?, repo?, branch?, types?, updated_since?,
                 include_inactive=false, limit=50, cursor?
memory_contexts  —
memory_get       memory_id
memory_put       content, memory_type, title="", workspace?, project?, repo?,
                 branch?, source?, supersedes?: list[str]
memory_update    memory_id, expected_version, title?, content?, memory_type?,
                 workspace?, project?, repo?, branch?, source?
memory_delete    memory_id
memory_restore   memory_id
```

- Results are JSON objects of the records above, with derived `state` included. `memory_put` returns `PutResult`; `memory_list` returns `ListPage`; `memory_contexts` returns `{"contexts": [...]}`.
- `memory_get` of an unknown ID is a `not_found` tool error (never a null result).
- Failures are MCP tool errors (`isError`) whose text ends in a JSON object: `{"error": "not_found", "id"}`, `{"error": "version_conflict", "current": Memory}`, `{"error": "duplicate_memory", "existing": Memory}`, `{"error": "invalid", "detail"}`. The payload carries the current/existing memory so the agent can retry without another `get`.
- `memory_put` without `source` uses MCP `clientInfo.name` when the client provides it.
- The server sets MCP `instructions` to the protocol text. `docs/agent-protocol.md` is the normative source and a test fails if it drifts from `adapters/mcp_server.py`.

## CLI

```text
membus doctor
membus put [--supersedes ID]... / get / update --expected-version N / delete / restore
membus search QUERY [--strict] [--type T]... [context flags]
membus list [--since TS] [--include-inactive] [--type T]... [context flags]
membus contexts
membus purge MEMORY_ID --yes
membus alias ALIAS CANONICAL
membus backup DEST
```

Output is JSON. Exit status: `0` success; `1` not found, version conflict, duplicate, or no-op delete/restore (with an `error` payload as in MCP); `2` invalid input. `update` treats an empty string as "clear" for title, context, and source.

## Evaluation

```bash
python tools/evaluate.py evals/golden_v1.json
python tools/evaluate.py evals/golden_v1.json --scale 100000 --max-p95-ms 50   # latency/noise run
```

New retrieval machinery must demonstrate measurable benefit before entering the runtime.
