"""MemBus core: shared SQLite memory with lexical retrieval."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA_VERSION = 2
_BUSY_TIMEOUT_MS = 5_000
_BEGIN_RETRY_ATTEMPTS = 5
_MAX_TITLE = 200
_MAX_CONTENT = 4_000
_MAX_QUERY_TOKENS = 32
_SIMILAR_LIMIT = 3
_MEMORY_TYPES = frozenset({
    "fact",
    "decision",
    "preference",
    "procedure",
    "incident",
    "note",
})
_TOKEN_RE = re.compile(r"[^\W_]+(?:[_-][^\W_]+)*", re.UNICODE)
_HAN_RUN_RE = re.compile(r"[㐀-鿿]+")
# Question words and glue that would otherwise fill an OR query's top-K.
# Deliberately excludes negations ("not", "no", "never"), which carry meaning.
_STOPWORDS = frozenset(
    "a an and are as at be been by can could do does for from had has have "
    "how i if in into is it its me my of on or our should so than that the "
    "their then there these they this those to us was we were what when "
    "where which who whom why will with would you your".split()
)
_CONTEXT_FIELDS = ("workspace", "project", "repo", "branch")
_USER_FIELDS = ("title", "content", "memory_type", *_CONTEXT_FIELDS, "source")
_MEMORY_COLUMNS = (
    "id", *_USER_FIELDS,
    "revision", "created_at", "updated_at", "deleted_at", "superseded_by",
)
_ACTIVE = "m.deleted_at IS NULL AND m.superseded_by IS NULL"

# One statement per entry so initialization can run inside a single
# BEGIN IMMEDIATE transaction (trigger bodies contain semicolons).
_SCHEMA = (
    """
    CREATE TABLE metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        revision INTEGER NOT NULL CHECK (revision >= 0),
        generation TEXT NOT NULL
    )
    """,
    "INSERT INTO metadata VALUES (1, 0, lower(hex(randomblob(16))))",
    """
    CREATE TABLE IF NOT EXISTS memories (
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
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS memories_active_hash
    ON memories(content_hash)
    WHERE deleted_at IS NULL AND superseded_by IS NULL
    """,
    "CREATE INDEX IF NOT EXISTS memories_updated ON memories(updated_at)",
    # Covering index: `contexts` is a pure index scan.
    """
    CREATE INDEX IF NOT EXISTS memories_context
    ON memories(workspace, project, repo)
    WHERE deleted_at IS NULL AND superseded_by IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS aliases (
        alias TEXT PRIMARY KEY,
        canonical TEXT NOT NULL
    )
    """,
    # Both FTS tables use memories.rid as rowid so sync is a rowid lookup.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
        title, content,
        tokenize = 'porter unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_trigram USING fts5(
        title, content,
        tokenize = 'trigram case_sensitive 0'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_ai
    AFTER INSERT ON memories
    WHEN new.deleted_at IS NULL AND new.superseded_by IS NULL BEGIN
        INSERT INTO memory_fts(rowid, title, content)
        VALUES (new.rid, new.title, new.content);
        INSERT INTO memory_trigram(rowid, title, content)
        VALUES (new.rid, new.title, new.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_au
    AFTER UPDATE OF title, content, deleted_at, superseded_by ON memories BEGIN
        DELETE FROM memory_fts WHERE rowid = old.rid;
        DELETE FROM memory_trigram WHERE rowid = old.rid;
        INSERT INTO memory_fts(rowid, title, content)
        SELECT new.rid, new.title, new.content
        WHERE new.deleted_at IS NULL AND new.superseded_by IS NULL;
        INSERT INTO memory_trigram(rowid, title, content)
        SELECT new.rid, new.title, new.content
        WHERE new.deleted_at IS NULL AND new.superseded_by IS NULL;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_ad
    AFTER DELETE ON memories BEGIN
        DELETE FROM memory_fts WHERE rowid = old.rid;
        DELETE FROM memory_trigram WHERE rowid = old.rid;
    END
    """,
)


class _Unset:
    """Marks an update field that was not passed (``None`` clears context)."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()


class NotFound(KeyError):
    """No memory with this ID."""

    def __init__(self, memory_id: str) -> None:
        super().__init__(memory_id)
        self.memory_id = memory_id


class RevisionConflict(Exception):
    """A memory changed revision or no longer permits the requested mutation."""

    def __init__(self, current: Memory) -> None:
        super().__init__(
            f"{current.id} is {current.state} at revision {current.revision}"
        )
        self.current = current


class SyncResetRequired(Exception):
    """The client must discard its cache and sync from revision zero."""

    def __init__(self, generation: str) -> None:
        super().__init__("discard the local cache and sync from revision zero")
        self.generation = generation


class DuplicateMemory(Exception):
    """The change would duplicate another active memory."""

    def __init__(self, existing: Memory) -> None:
        super().__init__(f"identical to active memory {existing.id}")
        self.existing = existing


@dataclass(frozen=True, slots=True)
class Memory:
    id: str
    title: str
    content: str
    memory_type: str
    workspace: str | None
    project: str | None
    repo: str | None
    branch: str | None
    source: str | None
    revision: int
    created_at: str
    updated_at: str
    deleted_at: str | None
    superseded_by: str | None

    @property
    def state(self) -> str:
        if self.deleted_at is not None:
            return "deleted"
        if self.superseded_by is not None:
            return "superseded"
        return "active"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state
        return payload


@dataclass(frozen=True, slots=True)
class PutResult:
    memory: Memory
    created: bool
    superseded: list[str]
    similar: list[Memory]

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory": self.memory.to_dict(),
            "created": self.created,
            "superseded": list(self.superseded),
            "similar": [memory.to_dict() for memory in self.similar],
        }


@dataclass(frozen=True, slots=True)
class MemoryRef:
    id: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class SyncPage:
    memories: list[Memory]
    next_revision: int
    generation: str
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "memories": [memory.to_dict() for memory in self.memories],
            "next_revision": self.next_revision,
            "generation": self.generation,
            "has_more": self.has_more,
        }


@dataclass(frozen=True, slots=True)
class ListPage:
    memories: list[Memory]
    next_cursor: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "memories": [memory.to_dict() for memory in self.memories],
            "next_cursor": self.next_cursor,
        }


@dataclass(frozen=True, slots=True)
class ContextCount:
    workspace: str | None
    project: str | None
    repo: str | None
    count: int


class MemBus:
    """User-owned SQLite memory bus."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._initialize()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def put(
        self,
        *,
        content: str,
        memory_type: str,
        title: str = "",
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        source: str | None = None,
        supersedes: Iterable[MemoryRef] = (),
    ) -> PutResult:
        values = _normalize_memory_values(
            title=title, content=content, memory_type=memory_type,
            workspace=workspace, project=project, repo=repo, branch=branch,
            source=source,
        )
        targets: dict[str, int] = {}
        for target in supersedes:
            if not isinstance(target, MemoryRef) or not isinstance(target.id, str) or not target.id:
                raise ValueError("supersedes requires MemoryRef(id, expected_revision)")
            _validate_revision(target.expected_revision)
            if target.id in targets and targets[target.id] != target.expected_revision:
                raise ValueError("conflicting revisions for the same supersedes ID")
            targets[target.id] = target.expected_revision
        digest = _content_hash(values)

        with self._transaction() as conn:
            now = _utc_now()
            existing = conn.execute(
                f"SELECT m.* FROM memories AS m "
                f"WHERE m.content_hash = ? AND {_ACTIVE}",
                (digest,),
            ).fetchone()
            if existing is not None:
                memory, created = _memory_from_row(existing), False
            else:
                memory = Memory(
                    id=f"mem_{uuid.uuid4().hex}", **values,
                    revision=_next_revision(conn),
                    created_at=now, updated_at=now,
                    deleted_at=None, superseded_by=None,
                )
                conn.execute(
                    """
                    INSERT INTO memories (
                        id, title, content, memory_type, workspace, project,
                        repo, branch, source, content_hash, revision,
                        created_at, updated_at, deleted_at, superseded_by
                    ) VALUES (
                        :id, :title, :content, :memory_type, :workspace,
                        :project, :repo, :branch, :source, :content_hash,
                        :revision, :created_at, :updated_at, :deleted_at,
                        :superseded_by
                    )
                    """,
                    {**_memory_params(memory), "content_hash": digest},
                )
                created = True

            for old_id, expected_revision in targets.items():
                if old_id == memory.id:
                    raise ValueError("a memory cannot supersede itself")
                current = _get_required(conn, old_id)
                # A lost response can be retried with the original reference.
                if current.deleted_at is None and current.superseded_by == memory.id:
                    continue
                _require_revision(current, expected_revision)
                if current.state != "active":
                    raise RevisionConflict(current)
                conn.execute(
                    """
                    UPDATE memories SET superseded_by = ?,
                        revision = ?, updated_at = ? WHERE id = ?
                    """,
                    (memory.id, _next_revision(conn), now, old_id),
                )

        similar = self._similar_to(memory, ignore=set(targets))
        return PutResult(
            memory=memory, created=created,
            superseded=list(targets), similar=similar,
        )

    def update(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        title: str | None = UNSET,
        content: str = UNSET,
        memory_type: str = UNSET,
        workspace: str | None = UNSET,
        project: str | None = UNSET,
        repo: str | None = UNSET,
        branch: str | None = UNSET,
        source: str | None = UNSET,
    ) -> Memory:
        """Correct an active memory.

        Omitted fields are unchanged; ``None`` clears title, context, or
        source. Raises RevisionConflict if the memory moved on and
        DuplicateMemory if the result would equal another active memory.
        """
        _validate_revision(expected_revision)
        passed = {
            "title": title, "content": content, "memory_type": memory_type,
            "workspace": workspace, "project": project, "repo": repo,
            "branch": branch, "source": source,
        }
        changes = {k: v for k, v in passed.items() if v is not UNSET}
        if not changes:
            raise ValueError("update requires at least one field")
        for required in ("content", "memory_type"):
            if required in changes and changes[required] is None:
                raise ValueError(f"{required} cannot be cleared")
        if changes.get("title", "") is None:
            changes["title"] = ""

        # Read and write under the same BEGIN IMMEDIATE lock.
        with self._transaction() as conn:
            current = _get_required(conn, memory_id)
            _require_revision(current, expected_revision)
            if current.state != "active":
                raise RevisionConflict(current)

            values = _normalize_memory_values(**{
                field: changes.get(field, getattr(current, field))
                for field in _USER_FIELDS
            })
            if all(getattr(current, f) == values[f] for f in _USER_FIELDS):
                return current

            digest = _content_hash(values)
            clash = conn.execute(
                f"SELECT m.* FROM memories AS m "
                f"WHERE m.content_hash = ? AND m.id <> ? AND {_ACTIVE}",
                (digest, memory_id),
            ).fetchone()
            if clash is not None:
                raise DuplicateMemory(_memory_from_row(clash))

            memory = replace(
                current, **values,
                revision=_next_revision(conn), updated_at=_utc_now(),
            )
            conn.execute(
                """
                UPDATE memories SET
                    title = :title, content = :content,
                    memory_type = :memory_type, workspace = :workspace,
                    project = :project, repo = :repo, branch = :branch,
                    source = :source, content_hash = :content_hash,
                    revision = :revision, updated_at = :updated_at
                WHERE id = :id
                """,
                {**_memory_params(memory), "content_hash": digest},
            )
        return memory

    def delete(self, memory_id: str, *, expected_revision: int) -> bool:
        """Soft-delete without changing any predecessor's supersession."""
        _validate_revision(expected_revision)
        with self._transaction() as conn:
            current = _get_required(conn, memory_id)
            _require_revision(current, expected_revision)
            if current.deleted_at is not None:
                return False
            now = _utc_now()
            conn.execute(
                """
                UPDATE memories SET deleted_at = ?, updated_at = ?,
                    revision = ? WHERE id = ?
                """,
                (now, now, _next_revision(conn), memory_id),
            )
            return True

    def restore(self, memory_id: str, *, expected_revision: int) -> bool:
        """Make a memory current again unless a live successor replaces it.

        Clears deletion, and clears supersession whose successor was deleted
        or purged. A memory superseded by a live successor stays superseded.
        Returns False when there is nothing to restore.
        """
        _validate_revision(expected_revision)
        with self._transaction() as conn:
            current = _get_required(conn, memory_id)
            _require_revision(current, expected_revision)
            successor = current.superseded_by
            if successor is not None and conn.execute(
                "SELECT 1 FROM memories WHERE id = ? AND deleted_at IS NULL",
                (successor,),
            ).fetchone() is None:
                successor = None
            if current.deleted_at is None and successor == current.superseded_by:
                return False

            if successor is None:
                row = conn.execute(
                    "SELECT content_hash FROM memories WHERE id = ?", (memory_id,),
                ).fetchone()
                clash = _active_clash(conn, row["content_hash"], memory_id)
                if clash is not None:
                    raise DuplicateMemory(_memory_from_row(clash))

            conn.execute(
                """
                UPDATE memories SET deleted_at = NULL, superseded_by = ?,
                    updated_at = ?, revision = ? WHERE id = ?
                """,
                (successor, _utc_now(), _next_revision(conn), memory_id),
            )
            return True

    def purge(self, memory_id: str) -> bool:
        """Physically remove a memory and invalidate every synchronization cursor."""
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE id = ?", (memory_id,)
            )
            if cursor.rowcount == 0:
                return False
            # Historical predecessor links remain superseded, even when the
            # successor's content has been erased. They never revive implicitly.
            conn.execute(
                "UPDATE metadata SET generation = ? WHERE singleton = 1",
                (uuid.uuid4().hex,),
            )
            return True

    def similar(self, memory_id: str) -> list[Memory]:
        """Advisory near-duplicates of an active memory among compatible ones."""
        memory = self.get(memory_id)
        if memory is None:
            raise NotFound(memory_id)
        if memory.state != "active":
            return []
        return self._similar_to(memory, ignore=set())

    def put_alias(self, alias: str, canonical: str) -> None:
        alias = _normalize_text(alias)
        canonical = _normalize_text(canonical)
        if not alias or not canonical:
            raise ValueError("alias and canonical must be non-empty")

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO aliases(alias, canonical)
                VALUES (?, ?)
                ON CONFLICT(alias) DO UPDATE SET canonical = excluded.canonical
                """,
                (alias, canonical),
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, memory_id: str) -> Memory | None:
        """Read a memory in any state; check ``state``/``superseded_by``."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,),
            ).fetchone()
        return _memory_from_row(row) if row is not None else None

    def list(
        self,
        *,
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        types: Iterable[str] = (),
        updated_since: str | None = None,
        include_inactive: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ListPage:
        """Browse active memories by known context fields, newest change first."""
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        context = _context_from(workspace, project, repo, branch)
        normalized_types = _normalize_types(types)

        where: list[str] = [] if include_inactive else [_ACTIVE]
        params: list[Any] = []
        _append_type_filter(where, params, normalized_types)
        _append_known_context(where, params, context)
        if updated_since is not None:
            # Wall-clock browsing filter for agents; sync is the exact feed.
            where.append("m.updated_at > ?")
            params.append(_normalize_timestamp(updated_since))
        # `+` stops the planner from walking the whole revision index when the
        # updated_at range index is the selective one.
        order = "m.revision DESC" if updated_since is None else "+m.revision DESC"
        if cursor is not None:
            where.append("m.revision < ?")
            params.append(_decode_cursor(cursor))

        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT m.* FROM memories AS m
                {'WHERE ' + ' AND '.join(where) if where else ''}
                ORDER BY {order}
                LIMIT ?
                """,
                [*params, limit + 1],
            ).fetchall()

        memories = [_memory_from_row(row) for row in rows[:limit]]
        next_cursor = (
            _encode_cursor(memories[-1].revision)
            if len(rows) > limit else None
        )
        return ListPage(memories=memories, next_cursor=next_cursor)

    def sync(
        self,
        *,
        after_revision: int = 0,
        generation: str | None = None,
        limit: int = 100,
    ) -> SyncPage:
        """Read latest states globally, including deleted/superseded memories.

        Start at zero; persist generation together with next_revision. This is
        eventual state synchronization, not a history or an atomic snapshot.
        """
        _validate_revision(after_revision, minimum=0)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if generation is not None and (not isinstance(generation, str) or not generation):
            raise ValueError("generation must be a nonempty string")
        if after_revision and generation is None:
            raise ValueError("generation is required when after_revision is nonzero")
        with self._connection() as conn:
            # Bind generation and rows to one read snapshot so concurrent purge
            # cannot return post-purge rows with a pre-purge generation.
            conn.execute("BEGIN")
            metadata = conn.execute("SELECT * FROM metadata WHERE singleton = 1").fetchone()
            if ((generation is not None and generation != metadata["generation"])
                    or after_revision > metadata["revision"]):
                raise SyncResetRequired(metadata["generation"])
            rows = conn.execute(
                "SELECT * FROM memories WHERE revision > ? ORDER BY revision LIMIT ?",
                (after_revision, limit + 1),
            ).fetchall()
        memories = [_memory_from_row(row) for row in rows[:limit]]
        return SyncPage(
            memories=memories,
            next_revision=memories[-1].revision if memories else after_revision,
            generation=metadata["generation"], has_more=len(rows) > limit,
        )

    def contexts(self) -> list[ContextCount]:
        """Distinct workspace/project/repo combinations of active memories."""
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT m.workspace, m.project, m.repo, COUNT(*) AS n
                FROM memories AS m WHERE {_ACTIVE}
                GROUP BY m.workspace, m.project, m.repo
                ORDER BY n DESC, m.workspace, m.project, m.repo
                """
            ).fetchall()
        return [
            ContextCount(row["workspace"], row["project"], row["repo"], row["n"])
            for row in rows
        ]

    def search(
        self,
        query: str,
        *,
        workspace: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        types: Iterable[str] = (),
        limit: int = 10,
        strict: bool = False,
    ) -> list[Memory]:
        query = _normalize_text(query)
        if not query:
            return []
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        context = _context_from(workspace, project, repo, branch)
        normalized_types = _normalize_types(types)
        scope = {"context": context, "types": normalized_types, "strict": strict}

        exact = self._exact_candidates(query, limit=limit, **scope)

        tokens = _significant_tokens(_extract_tokens(query))[:_MAX_QUERY_TOKENS]
        fts_query = _build_fts_or_query(
            _expand_tokens(tokens, self._alias_map(tokens))
        )
        fts = (
            self._fts_candidates(
                fts_query, limit=limit + len(exact), **scope,
            )
            if fts_query is not None
            else []
        )

        # Chinese has no word boundaries for unicode61, so recall comes from
        # the trigram index.
        trigrams = _cjk_ngrams(query, 3)[:12]
        cjk = (
            self._trigram_candidates(
                trigrams, limit=limit + len(exact) + len(fts), **scope,
            )
            if trigrams
            else []
        )

        partitions = [
            _partition_context(group, context) for group in (exact, fts, cjk)
        ]

        def compatible_ids() -> set[str]:
            return {m.id for preferred, _ in partitions for m in preferred}

        # Last resorts, only while compatible results are short of `limit`:
        # word-part substrings (Porter misses e.g. deploy/deployment) and
        # separated Chinese bigrams (e.g. 支付部署).
        stems = [
            token for token in tokens
            if len(token) >= 5 and not _HAN_RUN_RE.search(token)
        ][:8]
        if stems and len(compatible_ids()) < limit:
            partitions.append(_partition_context(
                self._trigram_candidates(
                    stems, limit=limit + len(compatible_ids()), **scope,
                ),
                context,
            ))
        bigrams = _cjk_ngrams(query, 2)[:12]
        if len(bigrams) >= 2 and len(compatible_ids()) < limit:
            partitions.append(_partition_context(
                self._cjk_bigram_candidates(
                    bigrams, limit=limit + len(compatible_ids()), **scope,
                ),
                context,
            ))

        results: list[Memory] = []
        seen: set[str] = set()
        ordered = [p for p, _ in partitions] + [f for _, f in partitions]
        for group in ordered:
            for memory in group:
                if memory.id in seen:
                    continue
                results.append(memory)
                seen.add(memory.id)
                if len(results) >= limit:
                    return results
        return results

    def backup(self, destination: str | Path) -> Path:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        with contextlib.closing(self._connect()) as source:
            with contextlib.closing(sqlite3.connect(destination_path)) as target:
                source.backup(target)
                # A restored/copied backup starts a separate sync generation.
                target.execute("UPDATE metadata SET generation = ? WHERE singleton = 1",
                               (uuid.uuid4().hex,))
                target.commit()
        return destination_path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            self._begin_with_retry(conn)
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _initialize(self) -> None:
        with contextlib.closing(self._connect()) as conn:
            self._ensure_wal(conn)
            if _schema_version(conn) == _SCHEMA_VERSION:
                return

        # Re-check under the write lock: another process may be initializing.
        with self._transaction() as conn:
            version = _schema_version(conn)
            if version == _SCHEMA_VERSION:
                return
            has_tables = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'memories'"
            ).fetchone()
            if version != 0 or has_tables:
                raise RuntimeError(
                    f"{self.path} was created by an incompatible MemBus "
                    f"schema (user_version={version}); export what you need "
                    "and recreate the database."
                )
            try:
                for statement in _SCHEMA:
                    conn.execute(statement)
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "fts5" in message or "tokenizer" in message:
                    raise RuntimeError(
                        "This SQLite build lacks FTS5 or its trigram "
                        "tokenizer (SQLite >= 3.34 is required)."
                    ) from exc
                raise
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _similar_to(self, memory: Memory, *, ignore: set[str]) -> list[Memory]:
        """Advisory near-duplicates among compatible active memories."""
        text = f"{memory.title} {memory.content}"
        candidates = self.search(
            text[:500],
            workspace=memory.workspace, project=memory.project,
            repo=memory.repo, branch=memory.branch,
            strict=True, limit=10,
        )
        mine = _similarity_tokens(text)
        similar: list[Memory] = []
        for other in candidates:
            if other.id == memory.id or other.id in ignore:
                continue
            theirs = _similarity_tokens(f"{other.title} {other.content}")
            shared = len(mine & theirs)
            smaller = min(len(mine), len(theirs))
            if shared >= 2 and smaller and shared / smaller >= 0.6:
                similar.append(other)
            if len(similar) >= _SIMILAR_LIMIT:
                break
        return similar

    def _exact_candidates(
        self,
        query: str,
        *,
        context: dict[str, str | None],
        types: tuple[str, ...],
        limit: int,
        strict: bool,
    ) -> list[Memory]:
        params: list[Any] = []
        where = [_ACTIVE]
        if len(query) >= 3:
            source = (
                "memory_trigram JOIN memories AS m "
                "ON m.rid = memory_trigram.rowid"
            )
            where.insert(0, "memory_trigram MATCH ?")
            params.append(_fts_phrase(query))
        elif _HAN_RUN_RE.search(query):
            # Short Han words have no token boundary for the word index and
            # are below one trigram, so this is the one remaining scan.
            source = "memories AS m"
            where.append(
                "(instr(lower(m.title), ?) > 0 "
                "OR instr(lower(m.content), ?) > 0)"
            )
            params.extend((query, query))
        else:
            # 1-2 ASCII characters (e.g. "uv", "pg"): whole-token matches are
            # served by the word index; substring scanning 100k rows is not
            # worth it.
            return []
        return self._run_candidates(
            source, where, params, order="m.revision DESC",
            context=context, types=types, limit=limit, strict=strict,
        )

    def _fts_candidates(
        self,
        match_query: str,
        *,
        context: dict[str, str | None],
        types: tuple[str, ...],
        limit: int,
        strict: bool,
    ) -> list[Memory]:
        return self._run_candidates(
            "memory_fts JOIN memories AS m ON m.rid = memory_fts.rowid",
            ["memory_fts MATCH ?", _ACTIVE], [match_query],
            order="bm25(memory_fts) ASC, m.id ASC",
            context=context, types=types, limit=limit, strict=strict,
        )

    def _trigram_candidates(
        self,
        phrases: list[str],
        *,
        context: dict[str, str | None],
        types: tuple[str, ...],
        limit: int,
        strict: bool,
    ) -> list[Memory]:
        return self._run_candidates(
            "memory_trigram JOIN memories AS m "
            "ON m.rid = memory_trigram.rowid",
            ["memory_trigram MATCH ?", _ACTIVE],
            [" OR ".join(_fts_phrase(phrase) for phrase in phrases)],
            order="bm25(memory_trigram) ASC, m.id ASC",
            context=context, types=types, limit=limit, strict=strict,
        )

    def _cjk_bigram_candidates(
        self,
        bigrams: list[str],
        *,
        context: dict[str, str | None],
        types: tuple[str, ...],
        limit: int,
        strict: bool,
    ) -> list[Memory]:
        # Separated short words, e.g. "支付部署": at least two bigrams must hit.
        hit_sql = " + ".join(
            "(instr(m.title, ?) > 0 OR instr(m.content, ?) > 0)"
            for _ in bigrams
        )
        hit_params = [gram for gram in bigrams for _ in (0, 1)]
        where = [_ACTIVE]
        params: list[Any] = []
        _append_type_filter(where, params, types)
        if strict:
            _append_strict_context(where, params, context)
        order_terms, order_params = _context_order(context)
        order = ", ".join([*order_terms, "m.hits DESC", "m.id ASC"])

        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT m.* FROM (
                    SELECT m.*, ({hit_sql}) AS hits
                    FROM memories AS m WHERE {' AND '.join(where)}
                ) AS m
                WHERE m.hits >= 2
                ORDER BY {order}
                LIMIT ?
                """,
                [*hit_params, *params, *order_params, limit],
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def _run_candidates(
        self,
        source: str,
        where: list[str],
        params: list[Any],
        *,
        order: str,
        context: dict[str, str | None],
        types: tuple[str, ...],
        limit: int,
        strict: bool,
    ) -> list[Memory]:
        where = list(where)
        params = list(params)
        _append_type_filter(where, params, types)
        if strict:
            _append_strict_context(where, params, context)
        order_terms, order_params = _context_order(context)
        order_by = ", ".join([*order_terms, order])

        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT m.* FROM {source}
                WHERE {' AND '.join(where)}
                ORDER BY {order_by}
                LIMIT ?
                """,
                [*params, *order_params, limit],
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def _alias_map(self, tokens: Iterable[str]) -> dict[str, str]:
        normalized = tuple(dict.fromkeys(token.casefold() for token in tokens if token))
        if not normalized:
            return {}

        placeholders = ",".join("?" for _ in normalized)
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT alias, canonical
                FROM aliases
                WHERE alias IN ({placeholders})
                """,
                normalized,
            ).fetchall()

        return {
            str(row["alias"]).casefold(): str(row["canonical"]).casefold()
            for row in rows
        }

    def _ensure_wal(self, conn: sqlite3.Connection) -> None:
        self._retry_sqlite(
            lambda: _require_wal(conn.execute("PRAGMA journal_mode = WAL").fetchone())
        )

    def _begin_with_retry(self, conn: sqlite3.Connection) -> None:
        self._retry_sqlite(lambda: conn.execute("BEGIN IMMEDIATE"))

    def _retry_sqlite(self, action: Callable[[], Any]) -> None:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(_BEGIN_RETRY_ATTEMPTS):
            try:
                action()
                return
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                last_error = exc
                if attempt + 1 >= _BEGIN_RETRY_ATTEMPTS:
                    break
                time.sleep(0.02 * (2**attempt))

        assert last_error is not None
        raise last_error


def _schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalize_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError(f"invalid timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _validate_revision(value: int, *, minimum: int = 1) -> None:
    if (not isinstance(value, int) or isinstance(value, bool)
            or not minimum <= value <= 2**63 - 1):
        raise ValueError(f"revision must be an integer between {minimum} and {2**63 - 1}")


def _next_revision(conn: sqlite3.Connection) -> int:
    conn.execute("UPDATE metadata SET revision = revision + 1 WHERE singleton = 1")
    return int(conn.execute("SELECT revision FROM metadata WHERE singleton = 1").fetchone()[0])


def _get_required(conn: sqlite3.Connection, memory_id: str) -> Memory:
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise NotFound(memory_id)
    return _memory_from_row(row)


def _require_revision(memory: Memory, expected_revision: int) -> None:
    if memory.revision != expected_revision:
        raise RevisionConflict(memory)


def _encode_cursor(revision: int) -> str:
    raw = json.dumps(revision).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> int:
    try:
        revision = json.loads(base64.urlsafe_b64decode(cursor))
        _validate_revision(revision)
        return revision
    except (ValueError, TypeError):
        pass
    raise ValueError("invalid cursor")


def _active_clash(
    conn: sqlite3.Connection, content_hash: str, own_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT m.* FROM memories AS m "
        f"WHERE m.content_hash = ? AND m.id <> ? AND {_ACTIVE}",
        (content_hash, own_id),
    ).fetchone()


def _normalize_memory_values(
    *,
    title: str,
    content: str,
    memory_type: str,
    workspace: str | None,
    project: str | None,
    repo: str | None,
    branch: str | None,
    source: str | None,
) -> dict[str, Any]:
    if not isinstance(content, str) or not isinstance(title, str):
        raise ValueError("title and content must be strings")
    content = content.strip()
    title = title.strip()
    if not content:
        raise ValueError("content must contain non-whitespace text")
    if len(content) > _MAX_CONTENT:
        raise ValueError(f"content exceeds {_MAX_CONTENT} characters")
    if len(title) > _MAX_TITLE:
        raise ValueError(f"title exceeds {_MAX_TITLE} characters")

    values: dict[str, Any] = {
        "title": title,
        "content": content,
        "memory_type": _memory_type(memory_type),
        "workspace": _clean_optional(workspace),
        "project": _clean_optional(project),
        "repo": _clean_optional(repo),
        "branch": _clean_optional(branch),
        "source": _clean_optional(source),
    }
    _validate_context(values)
    return values


def _content_hash(values: dict[str, Any]) -> str:
    parts = (
        _normalize_text(values[field] or "")
        for field in (
            "memory_type", "workspace", "project", "repo", "branch",
            "title", "content",
        )
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _memory_type(value: str) -> str:
    value = str(value)
    if value not in _MEMORY_TYPES:
        raise ValueError(
            f"Unsupported memory type: {value!r}; "
            f"expected one of {sorted(_MEMORY_TYPES)}"
        )
    return value


def _normalize_types(types: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_memory_type(value) for value in types))


def _context_from(
    workspace: str | None,
    project: str | None,
    repo: str | None,
    branch: str | None,
) -> dict[str, str | None]:
    context = {
        "workspace": _clean_optional(workspace),
        "project": _clean_optional(project),
        "repo": _clean_optional(repo),
        "branch": _clean_optional(branch),
    }
    # Read queries may know just one dimension (even branch without repo).
    # Stored memories still enforce branch-requires-repo at write time.
    return context


def _validate_context(values: dict[str, Any]) -> None:
    if values.get("branch") and not values.get("repo"):
        raise ValueError("branch requires repo")


def _append_type_filter(
    where: list[str],
    params: list[Any],
    types: tuple[str, ...],
) -> None:
    if types:
        placeholders = ",".join("?" for _ in types)
        where.append(f"m.memory_type IN ({placeholders})")
        params.extend(types)


def _append_strict_context(
    where: list[str],
    params: list[Any],
    context: dict[str, str | None],
) -> None:
    # An omitted context dimension must not admit narrower-scope memories.
    for column in _CONTEXT_FIELDS:
        value = context[column]
        if value is None:
            where.append(f"m.{column} IS NULL")
        else:
            where.append(f"(m.{column} IS NULL OR m.{column} = ?)")
            params.append(value)


def _append_known_context(
    where: list[str],
    params: list[Any],
    context: dict[str, str | None],
) -> None:
    """For discovery, omitted query dimensions are unknown, not NULL."""
    for column in _CONTEXT_FIELDS:
        value = context[column]
        if value is not None:
            where.append(f"(m.{column} IS NULL OR m.{column} = ?)")
            params.append(value)


def _context_order(
    context: dict[str, str | None],
) -> tuple[list[str], list[Any]]:
    """ORDER BY terms: fewest conflicts first, then most matches."""
    conflicts: list[str] = []
    matches: list[str] = []
    params: list[Any] = []
    for column in _CONTEXT_FIELDS:
        value = context[column]
        if value is None:
            continue
        conflicts.append(f"(m.{column} IS NOT NULL AND m.{column} <> ?)")
        matches.append(f"CASE WHEN m.{column} = ? THEN 1 ELSE 0 END")
        params.append(value)
    if not params:
        return [], []
    return (
        [f"({' + '.join(conflicts)}) ASC", f"({' + '.join(matches)}) DESC"],
        params + params,
    )


def _partition_context(
    memories: Iterable[Memory],
    context: dict[str, str | None],
) -> tuple[list[Memory], list[Memory]]:
    preferred: list[Memory] = []
    fallback: list[Memory] = []

    for memory in memories:
        target = fallback if _context_conflicts(memory, context) else preferred
        target.append(memory)

    return preferred, fallback


def _context_conflicts(
    memory: Memory,
    context: dict[str, str | None],
) -> bool:
    return any(
        context[column] is not None
        and getattr(memory, column) is not None
        and getattr(memory, column) != context[column]
        for column in _CONTEXT_FIELDS
    )


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _extract_tokens(query: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in _TOKEN_RE.finditer(query)))


def _significant_tokens(tokens: list[str]) -> list[str]:
    kept = [token for token in tokens if token not in _STOPWORDS]
    return kept or tokens


def _cjk_ngrams(query: str, size: int) -> list[str]:
    return list(dict.fromkeys(
        run[i:i + size]
        for match in _HAN_RUN_RE.finditer(query)
        for run in (match.group(0),)
        for i in range(len(run) - size + 1)
    ))


def _similarity_tokens(text: str) -> set[str]:
    folded = text.casefold()
    return set(_extract_tokens(folded)) | set(_cjk_ngrams(folded, 2))


def _expand_tokens(tokens: Iterable[str], aliases: dict[str, str]) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        for candidate in (token, aliases.get(token)):
            if candidate is not None and candidate not in seen:
                seen.add(candidate)
                expanded.append(candidate)
    return expanded


def _fts_phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _build_fts_or_query(tokens: Iterable[str]) -> str | None:
    parts = [_fts_phrase(token) for token in tokens if token]
    return " OR ".join(parts) if parts else None


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _memory_params(memory: Memory) -> dict[str, Any]:
    return {column: getattr(memory, column) for column in _MEMORY_COLUMNS}


def _memory_from_row(row: sqlite3.Row) -> Memory:
    return Memory(**{column: row[column] for column in _MEMORY_COLUMNS})


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _require_wal(row: sqlite3.Row | tuple[Any, ...] | None) -> None:
    if row is None or str(row[0]).casefold() != "wal":
        raise RuntimeError("SQLite refused WAL journal mode")
