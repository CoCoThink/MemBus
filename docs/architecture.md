# MemBus Architecture

Status: V1 design, implemented

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
```

There is one runtime core object. MCP and CLI are adapters over the same `MemBus`. Both FTS tables are derived from `memories` by triggers and index only active memories.

## Memory lifecycle

```text
              put
               │
               ▼
   ┌──────── active ◄──────────────┐
   │        │    ▲                 │
 delete  update  │ restore         │ delete of the
   │   (version  │                 │ superseding memory
   ▼     +1)     │                 │
 deleted ────────┘                 │
                                   │
 active ── put(supersedes=[id]) ──► superseded
```

- **update** is for corrections of the same memory: wording, typo, scope, type. It requires `expected_version`.
- **supersede** is for change in the world: a decision reversed, a procedure replaced, a fact no longer true. The new memory is a separate row; the old row keeps its content, points to its successor via `superseded_by`, and leaves search/list.
- **delete** of a memory that superseded others clears their `superseded_by`, reviving them. Restoring it afterwards does not re-supersede; the agent re-supersedes explicitly if wanted.
- **purge** (CLI only) removes a row physically, clears `superseded_by` references to it, and is the way to remove an accidentally stored secret.

Superseded rows form the semantic history of decisions; `get` on an old ID returns it with `superseded_by`, so an agent holding a stale ID is redirected.

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
put:      active row with same hash?  → return it (created=false)
          else insert; mark supersedes[] (version+1)
update:   version == expected_version? else VersionConflict(current)
          new hash collides with another active row? → DuplicateMemory(existing)
delete:   set deleted_at; clear superseded_by → this id
  ↓
FTS triggers
  ↓
commit
  ↓
put only: similar = top-3 compatible active matches (read, outside the write lock)
```

`content_hash` = SHA-256 of normalized `memory_type, workspace, project, repo, branch, title, content`. A partial unique index enforces it among active rows, so retries and parallel identical writes converge on one memory.

`similar` is advisory: it shows the writing agent near-duplicates (a lexical search filtered by token overlap, so unrelated results are not offered) so it can switch to `update` or `put(supersedes=...)`. MemBus never merges automatically.

## Runtime responsibilities

`MemBus` owns schema, connections, WAL, transactions and retry, lifecycle operations, aliases, retrieval, context filtering, and backup. MCP adapter owns tool descriptions, server instructions, and best-effort `source` default from MCP `clientInfo.name`.

There is no Database, Repository, Store interface, Service layer, or separate retrieval subsystem.

## Design decisions

Recorded from the design review; each entry is the problem, the chosen answer, and what was rejected.

| Problem | Decision | Rejected / deferred |
|---|---|---|
| Search-only access; agents cannot load "everything that applies" or see others' writes | `list` with context/type filters, `updated_since`, `include_inactive`, cursor | push subscriptions (MCP clients poll at session start anyway) |
| Context name fragmentation (`payment-api` vs `org/payment-api`) | `contexts` tool + naming convention in server instructions | automatic context inference from cwd (wrong for tunneled clients) |
| Lost updates across get→update round trips | `version` column; `update` requires `expected_version` | last-writer-wins; locking leases |
| Duplicate memories from retries / repeated recording | content-hash partial unique index (idempotent put) + advisory `similar` | caller idempotency keys (extra parameter agents forget); automatic merge |
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
| Clients with different trust share writable data | accepted: user trusts every connected client | read-only/append modes, ACL |
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
