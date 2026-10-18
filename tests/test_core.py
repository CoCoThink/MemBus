from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from membus import (
    DuplicateMemory,
    MemBus,
    Memory,
    MemoryRef,
    NotFound,
    RevisionConflict,
)
from membus.doctor import run_doctor


@pytest.fixture
def bus(tmp_path: Path) -> MemBus:
    return MemBus(tmp_path / "memory.db")


def put(bus: MemBus, **kwargs) -> Memory:
    kwargs.setdefault("memory_type", "note")
    return bus.put(**kwargs).memory


def ids(memories) -> list[str]:
    return [memory.id for memory in memories]


def assert_healthy(bus: MemBus) -> None:
    report = run_doctor(bus)
    assert report["ok"], [c for c in report["checks"] if not c["ok"]]


# ----------------------------------------------------------------------
# CRUD and metadata
# ----------------------------------------------------------------------


def test_crud(bus: MemBus) -> None:
    memory = put(
        bus,
        title="Generated client rule",
        content="generated/client.ts is generated from OpenAPI and must not be edited.",
        memory_type="procedure",
        project="payment",
        repo="payment-api",
    )

    assert bus.get(memory.id) == memory
    assert memory.revision == 1
    assert memory.state == "active"

    updated = bus.update(
        memory.id,
        expected_revision=1,
        content="generated/client.ts is generated from OpenAPI. Never edit it manually.",
    )
    assert "Never edit" in updated.content
    assert updated.revision == 2

    assert bus.delete(memory.id, expected_revision=bus.get(memory.id).revision) is True
    deleted = bus.get(memory.id)
    assert deleted is not None and deleted.state == "deleted"
    assert bus.delete(memory.id, expected_revision=bus.get(memory.id).revision) is False
    assert_healthy(bus)


def test_metadata_soft_delete_restore_and_fts(bus: MemBus) -> None:
    memory = put(bus, content="restore-test-marker", source="claude-code")
    assert memory.created_at == memory.updated_at
    assert memory.deleted_at is None
    assert datetime.fromisoformat(memory.created_at.replace("Z", "+00:00"))

    updated = bus.update(memory.id, expected_revision=1, source="codex", title="updated")
    assert updated.created_at == memory.created_at
    assert updated.updated_at >= memory.updated_at
    assert updated.source == "codex"

    assert bus.delete(memory.id, expected_revision=bus.get(memory.id).revision)
    assert bus.search("restore-test-marker") == []
    with pytest.raises(RevisionConflict):
        bus.update(memory.id, expected_revision=3, content="deleted")
    assert_healthy(bus)

    assert bus.restore(memory.id, expected_revision=bus.get(memory.id).revision)
    assert not bus.restore(memory.id, expected_revision=bus.get(memory.id).revision)
    revived = bus.get(memory.id)
    assert revived is not None and revived.state == "active"
    assert revived.created_at == memory.created_at
    assert ids(bus.search("restore-test-marker")) == [memory.id]
    assert_healthy(bus)


def test_backup_restores_data(tmp_path: Path) -> None:
    bus = MemBus(tmp_path / "source.db")
    memory = put(bus, content="This memory must survive a SQLite backup.",
                 memory_type="fact")

    bus.backup(tmp_path / "backup.db")

    backup = MemBus(tmp_path / "backup.db")
    assert backup.get(memory.id) == memory
    assert backup.sync().generation != bus.sync().generation


def test_validation(bus: MemBus) -> None:
    with pytest.raises(ValueError, match="branch requires repo"):
        bus.put(content="invalid branch context", memory_type="fact", branch="main")
    with pytest.raises(ValueError, match="content"):
        bus.put(content="   ", memory_type="fact")
    with pytest.raises(ValueError, match="4000"):
        bus.put(content="x" * 4001, memory_type="fact")
    bus.put(content="x" * 4000, memory_type="fact")
    with pytest.raises(ValueError, match="200"):
        bus.put(content="ok", title="t" * 201, memory_type="fact")
    # "failure" was merged into "incident".
    with pytest.raises(ValueError, match="Unsupported memory type"):
        bus.put(content="ok", memory_type="failure")
    with pytest.raises(ValueError, match="limit"):
        bus.search("anything", limit=101)


def test_incompatible_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="incompatible"):
        MemBus(path)

    fresh = tmp_path / "fresh.db"
    MemBus(fresh)
    MemBus(fresh)  # reopening a current database is fine


# ----------------------------------------------------------------------
# Idempotent put and similar
# ----------------------------------------------------------------------


def test_put_is_idempotent_for_identical_active_content(bus: MemBus) -> None:
    first = bus.put(content="Use uv for Python.", memory_type="preference",
                    title="Python tooling", workspace="dev")
    again = bus.put(content="  use  UV for python.  ", memory_type="preference",
                    title="python tooling", workspace="dev")

    assert first.created and not again.created
    assert again.memory.id == first.memory.id
    assert len(bus.list(workspace="dev").memories) == 1

    other_context = bus.put(content="Use uv for Python.", memory_type="preference",
                            title="Python tooling", workspace="home")
    other_type = bus.put(content="Use uv for Python.", memory_type="fact",
                         title="Python tooling", workspace="dev")
    assert other_context.created and other_type.created


def test_put_after_delete_or_supersede_creates_new_memory(bus: MemBus) -> None:
    first = put(bus, content="Same words either way")
    bus.delete(first.id, expected_revision=bus.get(first.id).revision)
    second = bus.put(content="Same words either way", memory_type="note")
    assert second.created and second.memory.id != first.id
    assert_healthy(bus)


def test_put_returns_similar_active_memories(bus: MemBus) -> None:
    existing = put(
        bus, memory_type="procedure", project="payment",
        title="Deploy checklist",
        content="Run database migrations before the payment deployment, then smoke test.",
    )
    put(bus, memory_type="procedure", project="payment",
        content="Rotate the signing key every quarter for audit compliance.")
    put(bus, memory_type="procedure", project="analytics",
        title="Deploy checklist",
        content="Run database migrations before the payment deployment, then smoke test.")

    result = bus.put(
        memory_type="procedure", project="payment",
        title="Deployment checklist",
        content="Run database migrations before the payment deployment and smoke test.",
    )

    assert result.created
    assert ids(result.similar) == [existing.id]


def test_similar_excludes_unrelated_and_superseded(bus: MemBus) -> None:
    old = put(bus, content="Staging deploys happen every Friday afternoon.")
    put(bus, content="Completely different topic about databases and indexes.")
    result = bus.put(
        content="Staging deploys happen every Monday morning now.",
        memory_type="note", supersedes=[MemoryRef(old.id, old.revision)],
    )
    assert result.similar == []


# ----------------------------------------------------------------------
# Supersession
# ----------------------------------------------------------------------


def test_supersede_replaces_memory_in_search_and_list(bus: MemBus) -> None:
    old = put(bus, memory_type="decision", project="payment",
              content="Settlement runs concurrently.")
    result = bus.put(
        memory_type="decision", project="payment",
        content="Settlement must run serially.", supersedes=[MemoryRef(old.id, old.revision)],
    )

    assert result.superseded == [old.id]
    assert ids(bus.search("settlement", project="payment")) == [result.memory.id]
    assert ids(bus.list(project="payment").memories) == [result.memory.id]

    stale = bus.get(old.id)
    assert stale is not None
    assert stale.state == "superseded"
    assert stale.superseded_by == result.memory.id
    assert stale.revision > result.memory.revision > old.revision
    assert stale.content == old.content

    both = bus.list(project="payment", include_inactive=True).memories
    assert {m.id for m in both} == {old.id, result.memory.id}
    assert_healthy(bus)


def test_supersede_is_atomic_and_validated(bus: MemBus) -> None:
    keep = put(bus, content="Existing active memory")
    gone = put(bus, content="Soon deleted")
    bus.delete(gone.id, expected_revision=bus.get(gone.id).revision)

    with pytest.raises(NotFound):
        bus.put(content="New memory A", memory_type="note",
                supersedes=[MemoryRef(keep.id, keep.revision), MemoryRef("mem_missing", 1)])
    with pytest.raises(RevisionConflict):
        bus.put(content="New memory B", memory_type="note", supersedes=[MemoryRef(gone.id, gone.revision)])
    with pytest.raises(ValueError, match="itself"):
        bus.put(content="Existing active memory", memory_type="note",
                supersedes=[MemoryRef(keep.id, keep.revision)])

    # Nothing from the failed puts leaked.
    assert ids(bus.list().memories) == [keep.id]
    assert bus.get(keep.id).revision == 1
    assert_healthy(bus)


def test_idempotent_put_still_applies_supersedes(bus: MemBus) -> None:
    old = put(bus, content="Old way")
    new = put(bus, content="New way")
    result = bus.put(content="New way", memory_type="note", supersedes=[MemoryRef(old.id, old.revision)])
    assert not result.created and result.memory.id == new.id
    assert bus.get(old.id).superseded_by == new.id


def test_deleting_successor_preserves_superseded_memories(bus: MemBus) -> None:
    old = put(bus, content="Original decision text")
    new = bus.put(content="Replacement decision text", memory_type="note",
                  supersedes=[MemoryRef(old.id, old.revision)]).memory

    assert bus.delete(new.id, expected_revision=bus.get(new.id).revision)
    stale = bus.get(old.id)
    assert stale is not None and stale.state == "superseded"
    assert stale.superseded_by == new.id
    assert bus.search("original decision") == []

    # Restoring the successor also leaves the predecessor untouched.
    assert bus.restore(new.id, expected_revision=bus.get(new.id).revision)
    assert bus.get(old.id) == stale
    assert_healthy(bus)


def test_restore_reactivates_predecessor_of_deleted_successor(bus: MemBus) -> None:
    old = put(bus, content="Deploy with fabric")
    new = bus.put(content="Deploy with ansible", memory_type="note",
                  supersedes=[MemoryRef(old.id, old.revision)]).memory

    # A live successor keeps the predecessor superseded.
    stale = bus.get(old.id)
    assert not bus.restore(old.id, expected_revision=stale.revision)
    assert bus.get(old.id) == stale

    bus.delete(new.id, expected_revision=new.revision)
    assert bus.restore(old.id, expected_revision=stale.revision)
    revived = bus.get(old.id)
    assert revived.state == "active" and revived.superseded_by is None
    assert revived.revision > stale.revision
    assert ids(bus.search("fabric")) == [old.id]
    assert not bus.restore(old.id, expected_revision=revived.revision)
    with pytest.raises(RevisionConflict):
        bus.restore(old.id, expected_revision=stale.revision)
    assert_healthy(bus)


def test_similar_flags_both_live_after_successor_is_restored(bus: MemBus) -> None:
    old = put(bus, content="Deploy with fabric", memory_type="procedure")
    new = bus.put(content="Deploy with ansible", memory_type="procedure",
                  supersedes=[MemoryRef(old.id, old.revision)]).memory
    bus.delete(new.id, expected_revision=new.revision)
    bus.restore(old.id, expected_revision=bus.get(old.id).revision)
    bus.restore(new.id, expected_revision=bus.get(new.id).revision)

    assert ids(bus.similar(new.id)) == [old.id]
    assert ids(bus.similar(old.id)) == [new.id]
    bus.delete(old.id, expected_revision=bus.get(old.id).revision)
    assert bus.similar(old.id) == []
    with pytest.raises(NotFound):
        bus.similar("mem_missing")


def test_restore_reactivates_predecessor_of_purged_successor(bus: MemBus) -> None:
    old = put(bus, content="Old statement")
    new = bus.put(content="Statement with a secret", memory_type="note",
                  supersedes=[MemoryRef(old.id, old.revision)]).memory
    assert bus.purge(new.id)
    assert bus.restore(old.id, expected_revision=bus.get(old.id).revision)
    assert bus.get(old.id).state == "active"


def test_restore_of_deleted_predecessor_of_deleted_successor(bus: MemBus) -> None:
    old = put(bus, content="Old statement")
    new = bus.put(content="New statement", memory_type="note",
                  supersedes=[MemoryRef(old.id, old.revision)]).memory
    bus.delete(old.id, expected_revision=bus.get(old.id).revision)
    bus.delete(new.id, expected_revision=new.revision)
    assert bus.restore(old.id, expected_revision=bus.get(old.id).revision)
    restored = bus.get(old.id)
    assert restored.state == "active" and restored.superseded_by is None


def test_restore_of_predecessor_rejects_duplicate(bus: MemBus) -> None:
    old = put(bus, content="Same text")
    new = bus.put(content="Replacement", memory_type="note",
                  supersedes=[MemoryRef(old.id, old.revision)]).memory
    bus.delete(new.id, expected_revision=new.revision)
    put(bus, content="Same text")
    stale = bus.get(old.id)
    with pytest.raises(DuplicateMemory):
        bus.restore(old.id, expected_revision=stale.revision)
    assert bus.get(old.id) == stale


def test_deleting_successors_does_not_rewrite_predecessors(bus: MemBus) -> None:
    first = put(bus, content="Duplicate-prone text")
    succ1 = bus.put(content="Successor one", memory_type="note",
                    supersedes=[MemoryRef(first.id, first.revision)]).memory
    second = put(bus, content="Duplicate-prone text")  # same content, new row
    assert second.id != first.id
    succ2 = bus.put(content="Successor two", memory_type="note",
                    supersedes=[MemoryRef(second.id, second.revision)]).memory

    first_before, second_before = bus.get(first.id), bus.get(second.id)
    bus.delete(succ1.id, expected_revision=succ1.revision)
    bus.delete(succ2.id, expected_revision=succ2.revision)
    assert bus.get(first.id) == first_before
    assert bus.get(second.id) == second_before
    assert_healthy(bus)


def test_restore_of_deleted_superseded_memory_stays_superseded(bus: MemBus) -> None:
    old = put(bus, content="Old statement")
    new = bus.put(content="New statement", memory_type="note",
                  supersedes=[MemoryRef(old.id, old.revision)]).memory
    assert bus.delete(old.id, expected_revision=bus.get(old.id).revision)
    assert bus.restore(old.id, expected_revision=bus.get(old.id).revision)
    restored = bus.get(old.id)
    assert restored.state == "superseded" and restored.superseded_by == new.id
    assert_healthy(bus)


def test_restore_rejects_duplicate_of_active_memory(bus: MemBus) -> None:
    first = put(bus, content="Will be duplicated")
    bus.delete(first.id, expected_revision=bus.get(first.id).revision)
    put(bus, content="Will be duplicated")
    with pytest.raises(DuplicateMemory):
        bus.restore(first.id, expected_revision=bus.get(first.id).revision)
    assert bus.get(first.id).state == "deleted"


def test_purge_removes_row_index_and_preserves_supersession(bus: MemBus) -> None:
    old = put(bus, content="Memory replaced by a secret one")
    secret = bus.put(content="api key is hunter2-marker", memory_type="note",
                     supersedes=[MemoryRef(old.id, old.revision)]).memory

    assert bus.purge(secret.id) is True
    assert bus.get(secret.id) is None
    assert bus.search("hunter2-marker") == []
    assert bus.get(old.id).state == "superseded"
    assert bus.get(old.id).superseded_by == secret.id
    assert bus.purge(secret.id) is False
    with bus._connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content LIKE '%hunter2%'"
        ).fetchone()[0] == 0
    assert_healthy(bus)


# ----------------------------------------------------------------------
# Optimistic concurrency
# ----------------------------------------------------------------------


def test_update_requires_current_revision(bus: MemBus) -> None:
    memory = put(bus, content="Original content", title="Original title")
    bus.update(memory.id, expected_revision=1, title="Edited by A")

    with pytest.raises(RevisionConflict) as conflict:
        bus.update(memory.id, expected_revision=1, content="Edited by B")
    assert conflict.value.current.title == "Edited by A"
    assert conflict.value.current.revision == 2

    final = bus.update(memory.id, expected_revision=2, content="Edited by B")
    assert (final.title, final.content, final.revision) == (
        "Edited by A", "Edited by B", 3,
    )


def test_update_noop_does_not_bump_revision(bus: MemBus) -> None:
    memory = put(bus, content="Stable", title="Same")
    assert bus.update(memory.id, expected_revision=1, title="Same") == memory
    with pytest.raises(ValueError, match="at least one"):
        bus.update(memory.id, expected_revision=1)


def test_update_clears_context_and_validates(bus: MemBus) -> None:
    memory = put(bus, content="Scoped", project="payment", repo="api", branch="dev")
    cleared = bus.update(memory.id, expected_revision=1, branch=None)
    assert cleared.branch is None and cleared.repo == "api"
    with pytest.raises(ValueError, match="branch requires repo"):
        bus.update(memory.id, expected_revision=2, repo=None, branch="x")
    with pytest.raises(ValueError, match="cannot be cleared"):
        bus.update(memory.id, expected_revision=2, content=None)
    with pytest.raises(ValueError, match="Unsupported memory type"):
        bus.update(memory.id, expected_revision=2, memory_type="nonsense")


def test_update_cannot_create_duplicate(bus: MemBus) -> None:
    a = put(bus, content="Alpha text")
    b = put(bus, content="Beta text")
    with pytest.raises(DuplicateMemory) as dup:
        bus.update(b.id, expected_revision=b.revision, content="Alpha text")
    assert dup.value.existing.id == a.id
    assert bus.get(b.id).content == "Beta text"


def test_update_rejects_superseded_and_deleted(bus: MemBus) -> None:
    old = put(bus, content="Old")
    bus.put(content="New", memory_type="note", supersedes=[MemoryRef(old.id, old.revision)])
    with pytest.raises(RevisionConflict):
        bus.update(old.id, expected_revision=2, content="Changed")
    with pytest.raises(NotFound):
        bus.update("mem_missing", expected_revision=1, content="Changed")


# ----------------------------------------------------------------------
# list / contexts
# ----------------------------------------------------------------------


def test_list_returns_only_applicable_context(bus: MemBus) -> None:
    glob = put(bus, content="global rule")
    ws = put(bus, content="workspace rule", workspace="work")
    proj = put(bus, content="project rule", workspace="work", project="payment")
    repo = put(bus, content="repo rule", workspace="work", project="payment",
               repo="api")
    other = put(bus, content="other project rule", workspace="work",
                project="analytics")

    assert ids(bus.list().memories) == [glob.id]
    got = {m.id for m in bus.list(workspace="work", project="payment").memories}
    assert got == {glob.id, ws.id, proj.id}
    got = {m.id for m in bus.list(workspace="work", project="payment",
                                  repo="api").memories}
    assert got == {glob.id, ws.id, proj.id, repo.id}
    assert other.id not in got


def test_list_type_filter_and_ordering(bus: MemBus) -> None:
    first = put(bus, content="first", memory_type="procedure")
    put(bus, content="second", memory_type="fact")
    third = put(bus, content="third", memory_type="procedure")

    listed = bus.list(types=["procedure"]).memories
    assert ids(listed) == [third.id, first.id]


def test_list_includes_inactive_only_when_requested(bus: MemBus) -> None:
    active = put(bus, content="active memory")
    gone = put(bus, content="deleted memory")
    bus.delete(gone.id, expected_revision=gone.revision)
    assert ids(bus.list().memories) == [active.id]
    assert {m.id: m.state for m in bus.list(include_inactive=True).memories} == {
        active.id: "active", gone.id: "deleted",
    }


def test_list_updated_since(bus: MemBus) -> None:
    before = put(bus, content="written before")
    mark = bus.get(before.id).updated_at
    after = put(bus, content="written after")
    bus.delete(before.id, expected_revision=before.revision)

    assert ids(bus.list(updated_since=mark).memories) == [after.id]
    assert {m.id: m.state for m in bus.list(
        updated_since=mark, include_inactive=True).memories} == {
        after.id: "active", before.id: "deleted",
    }
    # Naive timestamps are UTC; offsets are normalized.
    assert ids(bus.list(updated_since=mark.rstrip("Z")).memories) == [after.id]
    with pytest.raises(ValueError):
        bus.list(updated_since="yesterday")


def test_list_pagination_is_stable(bus: MemBus) -> None:
    created = [put(bus, content=f"memory number {n}") for n in range(7)]

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = bus.list(limit=3, cursor=cursor)
        seen.extend(ids(page.memories))
        pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break
    assert pages == 3
    assert seen == [m.id for m in reversed(created)]

    with pytest.raises(ValueError, match="cursor"):
        bus.list(cursor="not-a-cursor")
    with pytest.raises(ValueError, match="limit"):
        bus.list(limit=201)


def test_contexts_counts_active_memories(bus: MemBus) -> None:
    put(bus, content="a", workspace="work", project="payment", repo="api")
    put(bus, content="b", workspace="work", project="payment", repo="api")
    put(bus, content="c", workspace="personal")
    gone = put(bus, content="d", workspace="personal")
    bus.delete(gone.id, expected_revision=bus.get(gone.id).revision)

    counts = {(c.workspace, c.project, c.repo): c.count for c in bus.contexts()}
    assert counts == {("work", "payment", "api"): 2, ("personal", None, None): 1}
    assert bus.contexts()[0].count == 2


# ----------------------------------------------------------------------
# Search: context
# ----------------------------------------------------------------------


def test_matching_context_is_preferred_but_conflict_remains_retrievable(
    bus: MemBus,
) -> None:
    other = put(bus, content="PostgreSQL version for analytics is 15.",
                memory_type="fact", workspace="work", project="analytics")
    current = put(bus, content="PostgreSQL version for payment is 17.",
                  memory_type="fact", workspace="work", project="payment")

    result = ids(bus.search("PostgreSQL version", workspace="work",
                            project="payment"))

    assert result[0] == current.id
    assert other.id in result


def test_missing_workspace_broadens_search(bus: MemBus) -> None:
    project_memory = put(bus, content="context-containment-marker general payment guidance.",
                         project="payment")
    workspace_memory = put(bus, content="context-containment-marker personal payment guidance.",
                           workspace="personal", project="payment")

    assert set(ids(bus.search("context-containment-marker", project="payment"))) == {
        project_memory.id, workspace_memory.id,
    }


def test_more_specific_matching_context_is_preferred(bus: MemBus) -> None:
    project_memory = put(bus, content="specificity-marker project deployment guidance.",
                         memory_type="procedure", workspace="work", project="payment")
    repo_memory = put(bus, content="specificity-marker payment-api deployment guidance.",
                      memory_type="procedure", workspace="work", project="payment",
                      repo="payment-api")

    results = bus.search("specificity-marker", workspace="work",
                         project="payment", repo="payment-api")

    assert results[0].id == repo_memory.id
    assert project_memory.id in ids(results)


def test_strict_context_filters_before_limit(bus: MemBus) -> None:
    for n in range(12):
        put(bus, content=f"exact-marker unrelated-{n}", memory_type="fact",
            project="analytics")
    global_note = put(bus, content="exact-marker global", memory_type="fact")
    project_note = put(bus, content="exact-marker project", memory_type="fact",
                       project="payment")
    branch_note = put(bus, content="exact-marker branch", memory_type="fact",
                      project="payment", repo="api", branch="dev")

    result = bus.search("exact-marker", project="payment", strict=True, limit=2)
    assert {m.id for m in result} == {global_note.id, project_note.id}
    assert len(bus.search("exact-marker", project="payment", limit=100)) == 15
    assert ids(bus.search("exact-marker", strict=True)) == [global_note.id]
    assert branch_note.id not in ids(result)


def test_strict_fts_context_does_not_return_conflicting_hits(bus: MemBus) -> None:
    payment = put(bus, content="Payment production deployment smoke checklist",
                  memory_type="procedure", project="payment")
    put(bus, content="Analytics production deployment smoke checklist",
        memory_type="procedure", project="analytics")
    assert ids(bus.search("smoke deployment", strict=True, project="payment")) == [
        payment.id
    ]


def test_type_filter(bus: MemBus) -> None:
    procedure = put(bus, content="Run migrations before deployment.",
                    memory_type="procedure")
    put(bus, content="Deployment failed yesterday.", memory_type="incident")

    assert ids(bus.search("deployment", types=("procedure",))) == [procedure.id]


# ----------------------------------------------------------------------
# Search: lexical behavior
# ----------------------------------------------------------------------


def test_exact_identifier_is_retrievable(bus: MemBus) -> None:
    memory = put(
        bus, title="BUG-18472", memory_type="incident", project="payment",
        content="BUG-18472 was caused by reconcilePayment retrying after a partial settlement.",
    )
    assert bus.search("BUG-18472", project="payment")[0].id == memory.id
    assert bus.search("reconcilepayment retrying")[0].id == memory.id


def test_exact_substring_is_case_insensitive_and_handles_quotes(bus: MemBus) -> None:
    memory = put(bus, content='Set CONFIG_MODE to "strict" before running.')
    assert ids(bus.search('config_mode to "strict"')) == [memory.id]
    assert ids(bus.search('"strict" before')) == [memory.id]


def test_short_queries(bus: MemBus) -> None:
    memory = put(bus, content="Prefer uv over pip.", memory_type="preference")
    han = put(bus, content="部署前必须先备份数据库。", memory_type="procedure")
    # 1-2 ASCII characters match whole tokens through the word index.
    assert ids(bus.search("uv")) == [memory.id]
    assert bus.search("zz") == []
    assert bus.search("u") == []
    # Two Han characters have no token boundary: scanned as substrings.
    assert ids(bus.search("备份")) == [han.id]


def test_exact_matches_rank_newest_first(bus: MemBus) -> None:
    older = put(bus, content="exact-recency-marker version one")
    newer = put(bus, content="exact-recency-marker version two")
    assert ids(bus.search("exact-recency-marker")) == [newer.id, older.id]
    bus.update(older.id, expected_revision=1, title="touched")
    assert ids(bus.search("exact-recency-marker")) == [older.id, newer.id]


def test_english_inflection_and_word_part_matching(bus: MemBus) -> None:
    memory = put(bus, content="Deployment pipelines retry flaky steps automatically.")
    # Porter stemming: inflections and many derivations.
    assert ids(bus.search("retries")) == [memory.id]
    assert ids(bus.search("how does the pipeline work")) == [memory.id]
    # Porter alone misses deploy/deployment; the word-part fallback finds it.
    assert ids(bus.search("how do I deploy")) == [memory.id]
    assert bus.search("how do I cook") == []


def test_word_part_fallback_adds_derived_forms(bus: MemBus) -> None:
    stem_hit = put(bus, content="Redeployment notes for the platform team.")
    word_hit = put(bus, content="Please deploys-checklist the platform first.")
    exact_hit = put(bus, content="how to deploy platform changes safely")
    results = ids(bus.search("how to deploy platform"))
    assert results[0] == exact_hit.id
    assert set(results) == {stem_hit.id, word_hit.id, exact_hit.id}


def test_alias_expansion_recovers_fts_match(bus: MemBus) -> None:
    memory = put(bus, content="PostgreSQL connection pool was exhausted by worker concurrency.",
                 memory_type="incident", project="payment")
    bus.put_alias("pg", "postgresql")

    assert bus.search("pg", project="payment")[0].id == memory.id


def test_delete_removes_both_index_rows(bus: MemBus) -> None:
    memory = put(bus, content="temporary-marker")
    assert bus.delete(memory.id, expected_revision=bus.get(memory.id).revision) is True
    assert bus.search("temporary-marker") == []

    with bus._connection() as conn:
        for table in ("memory_fts", "memory_trigram"):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0


def test_chinese_reordered_query_uses_ngram_fallback(bus: MemBus) -> None:
    relevant = put(bus, content="生成的客户端代码需要通过 OpenAPI 更新。",
                   memory_type="procedure", project="payment")
    assert bus.search("如何更新客户端", project="payment", strict=True)[0].id == relevant.id
    assert bus.search("客户端", project="payment", strict=True)[0].id == relevant.id


def test_chinese_separated_two_character_terms_are_retrievable(bus: MemBus) -> None:
    relevant = put(bus, content="支付服务需要完成检查之后才能进行部署。",
                   memory_type="procedure", project="payment")
    assert bus.search("支付部署", project="payment", strict=True)[0].id == relevant.id


def test_chinese_fallback_weak_matches_scope_and_soft_delete(bus: MemBus) -> None:
    relevant = put(bus, content="生成的客户端代码需要通过 OpenAPI 更新。",
                   memory_type="procedure", project="payment")
    put(bus, content="如何检查日志收集系统。", project="payment")
    foreign = put(bus, content="客户端更新规则仅适用于内部分析平台。",
                  project="analytics")
    assert ids(bus.search("如何更新客户端", project="payment", strict=True)) == [
        relevant.id
    ]
    assert foreign.id in ids(bus.search("如何更新客户端", project="payment"))
    assert bus.delete(relevant.id, expected_revision=bus.get(relevant.id).revision)
    assert bus.search("如何更新客户端", project="payment", strict=True) == []
    assert bus.restore(relevant.id, expected_revision=bus.get(relevant.id).revision)
    assert bus.search("如何更新客户端", project="payment", strict=True)[0].id == relevant.id


def test_update_reindexes_both_indexes(bus: MemBus) -> None:
    memory = put(bus, content="alpha-before-marker 旧的部署流程")
    bus.update(memory.id, expected_revision=1, content="beta-after-marker 新的发布流程")
    assert bus.search("alpha-before-marker") == []
    assert bus.search("旧的部署流程") == []
    assert ids(bus.search("beta-after-marker")) == [memory.id]
    assert ids(bus.search("新的发布流程")) == [memory.id]
    assert_healthy(bus)
