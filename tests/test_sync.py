from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, current_thread

import pytest

from membus import MemBus, MemoryRef, RevisionConflict, SyncResetRequired


@pytest.fixture
def bus(tmp_path: Path) -> MemBus:
    return MemBus(tmp_path / "memory.db")


def test_revisions_are_global_and_noops_do_not_advance(bus: MemBus) -> None:
    first = bus.put(content="first", memory_type="note").memory
    second = bus.put(content="second", memory_type="note").memory
    edited = bus.update(first.id, expected_revision=first.revision, title="edited")
    assert first.revision < second.revision < edited.revision
    assert bus.update(first.id, expected_revision=edited.revision, title="edited") == edited
    assert bus.put(content="second", memory_type="note").memory == second
    assert bus.delete(second.id, expected_revision=second.revision)
    deleted = bus.get(second.id)
    assert bus.restore(second.id, expected_revision=deleted.revision)
    restored = bus.get(second.id)
    assert edited.revision < deleted.revision < restored.revision
    page = bus.sync()
    assert [m.id for m in page.memories] == [first.id, second.id]
    assert page.next_revision == restored.revision
    assert not page.has_more
    empty = bus.sync(after_revision=page.next_revision, generation=page.generation)
    assert empty.memories == [] and empty.next_revision == page.next_revision


def test_same_or_backwards_timestamps_cannot_hide_changes(bus: MemBus, monkeypatch) -> None:
    monkeypatch.setattr("membus.core._utc_now", lambda: "2030-01-01T00:00:02.000000Z")
    first = bus.put(content="first", memory_type="note").memory
    checkpoint = bus.sync()
    second = bus.put(content="same timestamp", memory_type="note").memory
    monkeypatch.setattr("membus.core._utc_now", lambda: "2030-01-01T00:00:01.000000Z")
    edited = bus.update(first.id, expected_revision=first.revision, content="clock went back")
    page = bus.sync(after_revision=checkpoint.next_revision, generation=checkpoint.generation)
    assert page.memories == [second, edited]
    assert edited.updated_at < second.updated_at
    assert bus.list().memories == [edited, second]


@pytest.mark.parametrize("operation", ["put", "delete", "restore"])
def test_late_committing_writer_is_visible_after_checkpoint(
    bus: MemBus, monkeypatch, operation: str,
) -> None:
    writer = MemBus(bus.path)
    target = bus.put(content="target", memory_type="note").memory
    if operation == "restore":
        bus.delete(target.id, expected_revision=target.revision)
        target = bus.get(target.id)
    original_transaction = writer._transaction
    ready, proceed = Event(), Event()

    @contextmanager
    def delayed_transaction():
        ready.set()
        if not proceed.wait(timeout=10):
            raise TimeoutError("writer was not released")
        with original_transaction() as conn:
            yield conn

    monkeypatch.setattr(writer, "_transaction", delayed_transaction)
    monkeypatch.setattr("membus.core._utc_now", lambda: (
        "2030-01-01T00:00:01.000000Z" if current_thread().name.startswith("late")
        else "2030-01-01T00:00:02.000000Z"
    ))

    def write():
        if operation == "put":
            return writer.put(content="late writer", memory_type="note").memory
        getattr(writer, operation)(target.id, expected_revision=target.revision)
        return writer.get(target.id)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="late") as executor:
        future = executor.submit(write)
        try:
            assert ready.wait(timeout=10)
            early = bus.put(content="early commit", memory_type="note").memory
            checkpoint = bus.sync()
            assert checkpoint.next_revision == early.revision
        finally:
            proceed.set()
        late = future.result(timeout=15)

    page = bus.sync(after_revision=checkpoint.next_revision, generation=checkpoint.generation)
    assert page.memories == [late]
    assert late.revision > early.revision and late.updated_at < early.updated_at


def test_sync_paging_converges_across_edits_deletions_and_scope_changes(bus: MemBus) -> None:
    first = bus.put(content="first", memory_type="note", project="own").memory
    second = bus.put(content="second", memory_type="note", project="own").memory
    third = bus.put(content="third", memory_type="note", project="own").memory
    page = bus.sync(limit=1)
    assert page.memories == [first] and page.has_more
    cache = {first.id: first}
    edited = bus.update(first.id, expected_revision=first.revision, title="edited after read")
    moved = bus.update(second.id, expected_revision=second.revision, project="other")
    bus.delete(third.id, expected_revision=third.revision)
    deleted = bus.get(third.id)
    seen = []
    while page.has_more:
        page = bus.sync(after_revision=page.next_revision, generation=page.generation, limit=1)
        for memory in page.memories:
            seen.append(memory)
            if memory.state == "active" and memory.project == "own":
                cache[memory.id] = memory
            else:
                cache.pop(memory.id, None)
    assert seen == [edited, moved, deleted]
    assert cache == {edited.id: edited}


def test_supersede_retry_is_a_noop_and_stale_target_rolls_back(bus: MemBus) -> None:
    old = bus.put(content="old", memory_type="note").memory
    reference = MemoryRef(old.id, old.revision)
    result = bus.put(content="replacement", memory_type="note", supersedes=[reference])
    checkpoint = bus.sync()
    retry = bus.put(content="replacement", memory_type="note", supersedes=[reference])
    assert not retry.created and retry.memory == result.memory
    assert retry.superseded == [old.id]
    assert bus.sync() == checkpoint

    keep = bus.put(content="keep", memory_type="note").memory
    stale = bus.put(content="stale", memory_type="note").memory
    corrected = bus.update(stale.id, expected_revision=stale.revision, content="corrected")
    before = bus.sync()
    with pytest.raises(RevisionConflict) as conflict:
        bus.put(content="must roll back", memory_type="note", supersedes=[
            MemoryRef(keep.id, keep.revision), MemoryRef(stale.id, stale.revision),
        ])
    assert conflict.value.current == corrected
    assert bus.sync() == before
    assert bus.get(keep.id) == keep
    # The failed insert and first supersession did not consume revisions.
    following = bus.put(content="following", memory_type="note").memory
    assert following.revision == before.next_revision + 1


@pytest.mark.parametrize("operation", ["delete", "restore"])
def test_delete_restore_reject_stale_revision(bus: MemBus, operation: str) -> None:
    memory = bus.put(content="original", memory_type="note").memory
    edited = bus.update(memory.id, expected_revision=memory.revision, content="corrected")
    with pytest.raises(RevisionConflict) as conflict:
        getattr(bus, operation)(memory.id, expected_revision=memory.revision)
    assert conflict.value.current == edited and bus.get(memory.id) == edited


def test_purge_invalidates_cursors_and_never_reuses_a_revision(bus: MemBus) -> None:
    memory = bus.put(content="erase", memory_type="note").memory
    old = bus.sync()
    assert bus.purge(memory.id)
    for revision in (0, old.next_revision):
        with pytest.raises(SyncResetRequired):
            bus.sync(after_revision=revision, generation=old.generation)
    fresh = bus.sync()
    assert not fresh.memories and fresh.generation != old.generation
    assert not bus.purge(memory.id)
    assert bus.sync().generation == fresh.generation
    next_memory = bus.put(content="new", memory_type="note").memory
    assert next_memory.revision > memory.revision


def test_generation_is_bound_to_database(bus: MemBus, tmp_path: Path) -> None:
    other = MemBus(tmp_path / "other.db")
    with pytest.raises(SyncResetRequired):
        other.sync(generation=bus.sync().generation)


def test_purge_between_metadata_and_rows_keeps_page_in_one_generation(bus: MemBus, monkeypatch) -> None:
    memory = bus.put(content="before purge", memory_type="note").memory
    old_generation = bus.sync().generation
    purger = MemBus(bus.path)
    original_connection = bus._connection
    purged = []

    @contextmanager
    def connection():
        with original_connection() as conn:
            def trace(statement):
                if statement.startswith("SELECT * FROM memories WHERE revision") and not purged:
                    purged.append(purger.purge(memory.id))
            conn.set_trace_callback(trace)
            yield conn

    monkeypatch.setattr(bus, "_connection", connection)
    page = bus.sync()
    assert purged == [True]
    assert page.generation == old_generation and page.memories == [memory]
    with pytest.raises(SyncResetRequired):
        bus.sync(after_revision=page.next_revision, generation=page.generation)


@pytest.mark.parametrize("remove_successor", ["delete", "purge"])
def test_restore_reactivates_when_successor_is_gone(bus: MemBus, remove_successor: str) -> None:
    old = bus.put(content="old", memory_type="note").memory
    new = bus.put(content="new", memory_type="note", supersedes=[MemoryRef(old.id, old.revision)]).memory
    old = bus.get(old.id)
    bus.delete(old.id, expected_revision=old.revision)
    deleted = bus.get(old.id)
    if remove_successor == "delete":
        bus.delete(new.id, expected_revision=new.revision)
    else:
        bus.purge(new.id)
    assert bus.restore(old.id, expected_revision=deleted.revision)
    restored = bus.get(old.id)
    assert restored.state == "active" and restored.superseded_by is None
    assert [m.id for m in bus.search("old")] == [old.id]
    page = bus.sync(after_revision=deleted.revision, generation=bus.sync().generation)
    assert [m.id for m in page.memories if m.id == old.id] == [old.id]


def test_retry_cannot_accept_a_deleted_supersedes_target(bus: MemBus) -> None:
    old = bus.put(content="old", memory_type="note").memory
    reference = MemoryRef(old.id, old.revision)
    bus.put(content="replacement", memory_type="note", supersedes=[reference])
    stale = bus.get(old.id)
    bus.delete(old.id, expected_revision=stale.revision)
    with pytest.raises(RevisionConflict):
        bus.put(content="replacement", memory_type="note", supersedes=[reference])


@pytest.mark.parametrize("kwargs", [
    {"after_revision": -1}, {"after_revision": True}, {"after_revision": "1"},
    {"after_revision": 2**63},
    {"after_revision": 1}, {"limit": 0}, {"limit": 201}, {"limit": True},
    {"generation": ""}, {"generation": 1},
])
def test_sync_validates_positions(bus: MemBus, kwargs) -> None:
    with pytest.raises(ValueError):
        bus.sync(**kwargs)
