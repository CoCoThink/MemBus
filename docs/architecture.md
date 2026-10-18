# MemBus Architecture

Status: implemented, schema 2

## Core

```text
Claude Code / Codex / Cursor / ChatGPT
                 │
             MCP / CLI
                 ▼
               MemBus
                 │
                 ▼
              SQLite
     ┌─────────┬─┴──────┬──────────────┐
     │         │        │              │
 memories   aliases  memory_fts   memory_trigram
                     (porter)     (trigram)
 metadata: global revision counter + synchronization generation
```

There is one runtime core object. MCP and CLI are adapters over the same `MemBus`. Both FTS tables are derived from `memories` by triggers and index only active memories.

## Memory lifecycle

```text
put → active ──update──► active
        │                 │
        └──supersede───────┴──► superseded

delete sets deleted_at; restore makes a memory current again
unless a live successor replaces it.
Deleting a successor never reactivates its predecessors implicitly.
```

- **update** is for corrections of the same memory: wording, typo, scope, type. It requires `expected_revision`.
- **supersede** is for change in the world: a decision reversed, a procedure replaced, a fact no longer true. The new memory is a separate row; the old row keeps its content, points to its successor via `superseded_by`, and leaves search/list.
- **delete** requires `expected_revision` and only sets deletion status; it never modifies predecessor links.
- **restore** requires `expected_revision` and is the one explicit way back to active: it clears deletion, and clears `superseded_by` when the successor was deleted or purged. A memory whose successor is live stays superseded. So after a wrong replacement is deleted, the topic can be recovered by restoring the predecessor, never by implicit revival.
- **purge** (CLI only) removes a row physically and rotates the synchronization generation. Historical predecessor links remain superseded; following such a successor can return not found.

Superseded rows form the semantic history of decisions; `get` on an old ID returns it with `superseded_by`, so an agent holding a stale ID is redirected.

## Revisions and synchronization

One metadata row contains the global revision counter and generation. Every actual memory mutation increments the counter and stores that value in the changed row, under `BEGIN IMMEDIATE`. Multiple rows changed by one put receive different revisions. Their changes and counter increments commit or roll back together. No-op mutations consume no revisions. Wall-clock timestamps are informational.

`sync(after_revision=0, generation=None, limit=100)` queries the current memories table by ascending revision, including inactive memories and all contexts. It returns `memories`, `next_revision`, `generation`, and `has_more`. Nonzero positions require their generation. Metadata and rows are read within one read transaction, so purge cannot mix generations in a page.

Clients apply results by ID, removing inactive or locally inapplicable entries, then persist generation and next_revision together. They continue while has_more and reuse that pair for polling. A modified row moves forward and will be read later or read again, so mutable records cannot fall behind the synchronization position. Initial loading starts at zero using the same interface.

Only the latest state of each memory is retained. Sync coalesces intermediate changes and provides eventual convergence; a page may contain part of a multi-memory write. There is no event log, request registry, or frozen multi-page snapshot. `list` is live browsing in descending revision order and is not a synchronization interface. Its `updated_since` timestamp filter serves LLM agents that cannot persist a sync position ("what changed in this repo since yesterday"); it is best effort, by wall clock, and cannot show memories moved out of the context.

Purge rotates generation atomically with deletion. A mismatched generation or a position ahead of the global counter raises `SyncResetRequired`; the client must discard its cache and start at zero without a generation. Missing purge targets do not rotate generation.

## Context

Context specificity is implicit:

```text
branch set     -> branch-specific
repo set       -> repo-specific
project set    -> project-specific
workspace set  -> workspace-specific
none set       -> global
```

A memory is **compatible** with a query context when every stored dimension is absent or equal to the supplied query dimension, and no stored dimension is set where the query omits it. A memory **conflicts** when some stored dimension differs from a supplied query dimension.

- `search` defaults to broad: compatible first, conflicting as fallback. `strict=True` returns compatible only.
- `list` always returns compatible only: it answers "what applies here".
- `contexts` returns the distinct workspace/project/repo values in use so agents reuse names instead of fragmenting context.

For non-coding use, `workspace` is the general domain (`personal`, `health`, `writing`, ...); project/repo/branch stay empty.

## Retrieval

```text
query
  ├─ exact substring ──── memory_trigram phrase MATCH (query ≥ 3 chars)
  │                       1-2 Han chars: instr scan; 1-2 ASCII chars: word index only
  ├─ word FTS ─────────── tokens → drop stopwords → aliases → memory_fts (porter) BM25
  ├─ Chinese ──────────── Han 3-grams → memory_trigram MATCH, BM25
  └─ last resorts, only while compatible results < limit:
        word parts (≥ 5 chars) → memory_trigram, BM25      deploy → deployment
        Han 2-grams → instr scan, ≥ 2 hits                  支付部署
          │
          ▼
 context tier: compatible │ conflicting (broad only)
          │
          ▼
 within tier: exact (newest first) → word → Chinese → word parts → bigrams
          │
          ▼
        Top-K
```

Context filtering happens in SQL before LIMIT. There is no numeric fusion layer; the only tie-break signal added is recency inside the exact class, where previously ordering was by random ID.

## Writes

```text
validate + normalize + content_hash
  ↓
BEGIN IMMEDIATE
  ↓
put:      active row with same hash?  → reuse it (created=false)
          else insert with a new global revision
          for each {id, expected_revision} supersedes target:
            already superseded by this replacement and not deleted? → skip
            otherwise require active + matching revision, then supersede
update:   revision == expected_revision and active? else RevisionConflict(current)
          new hash collides with another active row? → DuplicateMemory(existing)
delete:   require matching revision; set deleted_at, allocate revision
restore:  require matching revision; clear deleted_at, and superseded_by
          if its successor is deleted/purged; must not duplicate an active row
  ↓
FTS triggers
  ↓
commit
  ↓
put only: similar = top-3 compatible active matches (read, outside the write lock)
```

`content_hash` = SHA-256 of normalized `memory_type, workspace, project, repo, branch, title, content`. A partial unique index enforces it among active rows, so parallel identical writes converge on one memory. Supersedes retries recognize an already-established link before checking the original revision. A stale target rolls back the entire put, including any inserted replacement and earlier targets. Success is determined from current state; original responses are not stored for replay.

`similar` is advisory: it shows the writing agent near-duplicates (a lexical search filtered by token overlap, so unrelated results are not offered) so it can switch to `update` or `put(supersedes=...)`. MemBus never merges automatically.

## Runtime responsibilities

`MemBus` owns schema, connections, WAL, transactions and retry, lifecycle operations, aliases, retrieval, context filtering, and backup. MCP adapter owns tool descriptions, server instructions, and `source`, set on every put/update from MCP `clientInfo.name` (the client software, not the model) so it names the last writer; agents cannot pass it.

There is no Database, Repository, Store interface, Service layer, or separate retrieval subsystem.

## Design decisions

Recorded from the design review; each entry is the problem, the chosen answer, and what was rejected.

| Problem | Decision | Rejected / deferred |
|---|---|---|
| Agents need both context browsing and reliable cache synchronization | `list` for browsing (with best-effort `updated_since` for agents without persistent state); unfiltered ascending-revision `sync` for latest states | timestamp watermarks as the sync contract, event logs, frozen snapshots |
| Sync instructions in every agent's server instructions, though only programs with persistent state can use them | sync protocol lives in the `memory_sync` tool description only | — |
| Deleting a wrong successor left its predecessor superseded with no way back | explicit `restore` of the predecessor reactivates it when its successor is deleted/purged | implicit revival on delete (silently resurrects outdated memories) |
| Context name fragmentation (`payment-api` vs `org/payment-api`) | `contexts` tool + naming convention in server instructions | automatic context inference from cwd (wrong for tunneled clients) |
| Lost work across read→mutation round trips | one global `revision` token; required for update/delete/restore and every supersedes target | optional concurrency checks; separate per-memory versions |
| Duplicate memories from retries / repeated recording | content-hash partial unique index + state-based supersedes retry + advisory `similar` | request registry and exact response replay; automatic merge |
| Stale decisions returned alongside current ones | explicit supersession; superseded rows excluded from retrieval | recency-weighted scoring; validity intervals |
| Exact class ordered by random ID | newest first | general recency boost |
| No English stemming (`deploy` ≠ `deployment`) | Porter on word FTS (retry/retries, migrate/migration, cache/caching), plus a last-resort word-part substring pass | custom tokenizers. **Measured limit:** Porter itself stems `deploy`→`deploi` but `deployment`→`deploy`, so derivations ending in -y (deploy/deployment, employ/employment) and fail/failure need the fallback or an alias; under dense matches the fallback only runs when results are short, so such a query can lose to noise at 100k |
| OR queries scoring huge posting lists (`how`, `should`) | drop English stopwords (negations kept) | stemming-only; df-based pruning (deferred; measured P95 54 → 26 ms from stopwords alone) |
| Full-table scans for substring and Chinese n-grams | trigram FTS index; Han bigram scan only as last resort; 1-2 ASCII-char queries use the word index only | jieba/segmenter dependency; embeddings |
| FTS maintenance cost grows with the store | FTS rowid = `memories.rid`; update/delete/restore are rowid operations (< 1 ms at 100k) | `UNINDEXED memory_id` column (full FTS scan per delete) |
| Unbounded memory size wastes agent context | content ≤ 4000 chars; search returns full memories | snippets (forces extra get round-trips) |
| `incident` vs `failure` ambiguity | merge into `incident`; 6 types with definitions | free-form types |
| Thin tool descriptions; agents guess the protocol | normative tool descriptions + server instructions ([agent protocol](agent-protocol.md)) | — |
| Untyped `update(changes: dict)` | explicit optional typed parameters | — |
| Accidentally stored secret cannot be removed | CLI `purge` | MCP purge (irreversible; human decision) |
| Purge cannot be discovered by scanning current rows | rotate generation and require a complete cache reload | tombstone/event-history tables |
| Clients with different trust share writable data | accepted: user trusts every connected client | read-only/append modes, ACL |
| An agent that read injected content can plant a memory every other agent loads | server instructions frame memories as agent-written notes that never alone justify risky actions; MCP `source` names the last writing client and cannot be set by the agent | authenticated provenance, per-memory approval |
| Restoring a deleted successor after its predecessor was restored leaves two live answers | `memory_restore` returns `similar`, like put | tracking past supersession links; automatic re-supersede |
| Recovering from a bad edit | supersession for semantic changes; `backup` for disaster | revision-history table (revisit if bad `update`s are observed) |

## Deferred complexity

Add new machinery only for a measured failure:

- revision history
- runtime retrieval logs
- batch retrieval
- calibrated ranking, query rewrite/rerank
- vector or graph indexes
- automatic extraction
- ACL/multi-user gateway
- distributed storage
