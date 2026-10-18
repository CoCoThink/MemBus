# MemBus Technical Specification

Status: implemented in `src/membus`, schema 2

## Runtime and storage

The runtime consists of `core.py`, `cli.py`, `doctor.py`, and the optional MCP adapter. Both adapters call the same `MemBus`. `tools/evaluate.py` is repository tooling. SQLite requires FTS5 and its trigram tokenizer (SQLite >= 3.34).

The complete DDL lives in `core.py`. The canonical tables are:

```sql
CREATE TABLE metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    generation TEXT NOT NULL
);
-- Initialized once with revision 0 and a random generation.

CREATE TABLE memories (
    rid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    workspace TEXT, project TEXT, repo TEXT, branch TEXT,
    source TEXT,
    content_hash TEXT NOT NULL,
    revision INTEGER NOT NULL UNIQUE CHECK (revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    superseded_by TEXT
);

CREATE UNIQUE INDEX memories_active_hash ON memories(content_hash)
    WHERE deleted_at IS NULL AND superseded_by IS NULL;
CREATE INDEX memories_context ON memories(workspace, project, repo)
    WHERE deleted_at IS NULL AND superseded_by IS NULL;
CREATE TABLE aliases (alias TEXT PRIMARY KEY, canonical TEXT NOT NULL);
```

`memory_fts` uses Porter/unicode61 and `memory_trigram` uses case-insensitive trigram tokenization. Triggers keep both equal to the active memories, keyed by `rid`. They are derived and rebuildable. The unique revision index serves ascending sync and descending list queries.

`PRAGMA user_version` is 2. Incompatible databases are refused; there is no migration or compatibility layer. Initialization is atomic and rechecks schema version under the write lock.

## Records and validation

`Memory` exposes all memories columns except `rid` and `content_hash`, plus derived `state`:

```text
deleted     deleted_at is set
superseded  deleted_at is absent and superseded_by is set
active      neither is set
```

Title is at most 200 characters; normalized content must contain 1–4000 characters. Memory type is `fact | decision | preference | procedure | incident | note`. Branch requires repo. Context/source can be absent. Content hash covers normalized type, context, title, and content; source is excluded.

Revisions are positive integers. They are global tokens, so a memory's revision may jump by more than one. Every actual memory mutation allocates a new revision inside its write transaction. Timestamps are UTC ISO-8601 with microseconds and `Z`; they provide display metadata and need not increase.

## Core API

```python
MemoryRef(id: str, expected_revision: int)

MemBus(path)
bus.put(*, content, memory_type, title="", workspace=None, project=None,
        repo=None, branch=None, source=None, supersedes=()) -> PutResult
bus.get(memory_id) -> Memory | None
bus.update(memory_id, *, expected_revision, title=..., content=...,
           memory_type=..., workspace=..., project=..., repo=...,
           branch=..., source=...) -> Memory
bus.delete(memory_id, *, expected_revision) -> bool
bus.restore(memory_id, *, expected_revision) -> bool
bus.purge(memory_id) -> bool
bus.list(*, workspace=None, project=None, repo=None, branch=None,
         types=(), updated_since=None, include_inactive=False, limit=50,
         cursor=None) -> ListPage
bus.sync(*, after_revision=0, generation=None, limit=100) -> SyncPage
bus.search(query, *, workspace=None, project=None, repo=None, branch=None,
           types=(), limit=10, strict=False) -> list[Memory]
bus.contexts() -> list[ContextCount]
bus.put_alias(alias, canonical) -> None
bus.backup(destination) -> Path
```

`supersedes` contains `MemoryRef` values in Python and `{id, expected_revision}` objects over MCP. Duplicate identical references are collapsed; conflicting revisions for one ID are invalid.

```text
PutResult: memory, created, superseded [IDs], similar [up to 3 memories]
ListPage: memories, next_cursor
SyncPage: memories, next_revision, generation, has_more
ContextCount: workspace, project, repo, count
```

## Write transactions

Writes use `BEGIN IMMEDIATE`, WAL, a busy timeout, and bounded retries. Counter increments, memory changes, and FTS triggers commit or roll back together. Each changed row gets its own revision; no-op operations allocate none.

`put` finds an active content-hash match or inserts a new memory. For every supersedes target:

1. Reject self-supersession; missing IDs raise `NotFound`.
2. If the target is not deleted and already points to this replacement, accept it without checking the old revision or modifying it. This handles a lost response followed by an identical retry.
3. Otherwise require the supplied revision and active state, then set `superseded_by` and allocate a revision. Any mismatch raises `RevisionConflict(current)` and rolls back the entire put.

After commit, put computes advisory `similar` matches with the existing lexical search and overlap filter. Content deduplication and state-based retries do not store original responses. Intervening changes can cause a retried request to conflict.

`update` requires a matching revision and active state. At least one field is required. Omitted fields are unchanged; Python `None` clears title/context/source, but cannot clear content/type. An unchanged result returns the current memory without a new revision. A hash collision raises `DuplicateMemory(existing)`.

`delete` requires a matching revision and sets deletion timestamp plus a fresh revision. It returns false if already deleted at the supplied revision. `restore` requires a matching revision and makes the memory current unless a live successor replaces it: it clears deletion, and clears `superseded_by` when the successor is deleted or purged. A deleted memory with a live successor becomes superseded. It returns false when nothing changes (active, or superseded by a live successor). Restoring an unsuperseded row must not duplicate an active memory. Missing IDs raise `NotFound`.

Neither operation modifies other memories. A business reversal is recorded explicitly as a new memory. Regular update/delete/restore retries carrying a stale token return a conflict with current state.

`purge` physically removes a row and rotates generation in the same transaction. It returns false for an absent row without rotating generation. Predecessor links remain superseded and may refer to a purged successor. Purge is exposed only through CLI with `--yes`.

## Synchronization

`sync` reads current rows globally in ascending revision order. Deleted and superseded rows are always included. It intentionally has no context/type filters: clients must receive scope changes so they can evict previously applicable memories.

Start at revision zero without generation. For every page, apply each memory by ID; remove it from the active cache if inactive or inapplicable. Persist generation and next_revision together after applying the page. Continue while has_more and reuse the saved pair for later polls.

Nonzero after_revision requires generation. Metadata and rows are read in one read transaction. The returned next_revision is the last returned row's revision, or the supplied position for an empty page. It never skips unreturned rows. Limits are 1–200.

A modified record receives a larger revision, so it remains ahead of the reader or appears again. This provides eventual convergence on latest states, including during initial loading. Intermediate edits may be coalesced; pages can split a multi-row write and do not represent a frozen snapshot. No change-history, request-registry, or snapshot tables are maintained.

A mismatched generation or position ahead of the global counter raises `SyncResetRequired(generation)`. Discard the local cache and restart at zero without a generation. A purge therefore invalidates even an empty-cache cursor at revision zero.

Backups receive a fresh generation so restoring or independently opening one requires a fresh cache load. Source generation and all memory contents/revisions remain unchanged.

## Browsing and retrieval

`list` applies strict context compatibility and optional type and `updated_since` filters, adding inactive records when requested. `updated_since` compares wall-clock `updated_at` (assigned inside the write transaction) and is a browsing aid for agents without a saved sync position, not a synchronization guarantee. It sorts by descending revision with an opaque cursor containing the last revision. Concurrent updates can move records ahead of a browse cursor; use sync for complete cache loading and polling.

Search uses the existing lexical pipeline, active memories only. Context compatibility sorts first, followed by exact substring matches, word FTS/BM25, Chinese trigram matches, and bounded word-part/bigram fallbacks. Exact matches use descending revision as their recency signal. Context filtering happens before LIMIT. See [architecture](architecture.md#retrieval) and [evaluation](evaluation.md).

## Adapters and errors

MCP exposes `memory_search`, `memory_list`, `memory_sync`, `memory_contexts`, `memory_get`, `memory_put`, `memory_update`, `memory_delete`, and `memory_restore`. Mutations accept expected_revision where required. An empty string clears an optional field in CLI/MCP update; omitted fields stay unchanged. MCP put/update take no `source`; the adapter sets it to the MCP client name when available, so it tracks the last writer. CLI and Python accept `source` directly. MCP and CLI restore also return `similar` for a memory that became active; `bus.similar(memory_id)` exposes the same advisory check.

Errors carry structured payloads:

```text
not_found            id
revision_conflict    current Memory
duplicate_memory     existing Memory
sync_reset_required  generation, hint
invalid              detail
```

The agent must inspect current state and reconsider its intent on a revision conflict. It must not blindly replace the token and reissue a stale change. The [agent protocol](agent-protocol.md) is checked against adapter instructions and tool descriptions.

```text
membus put --content TEXT --memory-type TYPE [--supersedes ID:REVISION]...
membus update ID --expected-revision N [memory fields]
membus delete ID --expected-revision N
membus restore ID --expected-revision N
membus list [context/type filters] [--updated-since TS] [--include-inactive] [--limit N] [--cursor TOKEN]
membus sync [--after-revision N] [--generation TOKEN] [--limit N]
membus purge ID --yes
membus get ID / contexts / doctor / search QUERY / backup DESTINATION
```

CLI emits JSON. Exit 0 means success; 1 covers domain conflicts, missing records, sync reset, or a false delete/restore/purge result; 2 covers invalid input.

## Validation

Doctor checks WAL, SQLite integrity, FTS/trigram consistency, self-supersession, active duplicates, global revision metadata, and write readiness. Missing successors are reported as information because purge permits them.

Tests cover multi-process revision allocation, stale mutations, atomic rollback, supersedes retries, clock reversal/equal timestamps, paging through concurrent state changes, generation mismatch, and CLI/MCP round trips. Retrieval quality gates remain in the golden evaluation tooling.
