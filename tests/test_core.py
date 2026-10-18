from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event

import pytest

from membus import MemBus


@pytest.fixture
def bus(tmp_path: Path) -> MemBus:
    return MemBus(tmp_path / "memory.db")


def test_crud(bus: MemBus) -> None:
    memory = bus.put(
        title="Generated client rule",
        content="generated/client.ts is generated from OpenAPI and must not be edited.",
        memory_type="procedure",
        project="payment",
        repo="payment-api",
    )

    assert bus.get(memory.id) == memory

    updated = bus.update(
        memory.id,
        content="generated/client.ts is generated from OpenAPI. Never edit it manually.",
    )
    assert "Never edit" in updated.content

    assert bus.delete(memory.id) is True
    assert bus.get(memory.id) is None
    assert bus.delete(memory.id) is False


def test_matching_context_is_preferred_but_conflict_remains_retrievable(
    bus: MemBus,
) -> None:
    other = bus.put(
        content="PostgreSQL version for analytics is 15.",
        memory_type="fact",
        workspace="work",
        project="analytics",
    )
    current = bus.put(
        content="PostgreSQL version for payment is 17.",
        memory_type="fact",
        workspace="work",
        project="payment",
    )

    results = bus.search(
        "PostgreSQL version",
        workspace="work",
        project="payment",
    )
    ids = [memory.id for memory in results]

    assert ids[0] == current.id
    assert other.id in ids




def test_missing_workspace_broadens_search(bus: MemBus) -> None:
    project_memory = bus.put(
        content="context-containment-marker general payment guidance.",
        memory_type="note",
        project="payment",
    )
    workspace_memory = bus.put(
        content="context-containment-marker personal payment guidance.",
        memory_type="note",
        workspace="personal",
        project="payment",
    )

    ids = {
        memory.id
        for memory in bus.search(
            "context-containment-marker",
            project="payment",
        )
    }

    assert ids == {project_memory.id, workspace_memory.id}


def test_missing_repo_broadens_search(bus: MemBus) -> None:
    project_memory = bus.put(
        content="repo-containment-marker project deployment guidance.",
        memory_type="procedure",
        workspace="work",
        project="payment",
    )
    repo_memory = bus.put(
        content="repo-containment-marker payment-api deployment guidance.",
        memory_type="procedure",
        workspace="work",
        project="payment",
        repo="payment-api",
    )

    ids = {
        memory.id
        for memory in bus.search(
            "repo-containment-marker",
            workspace="work",
            project="payment",
        )
    }

    assert ids == {project_memory.id, repo_memory.id}


def test_more_specific_matching_context_is_preferred(bus: MemBus) -> None:
    project_memory = bus.put(
        content="specificity-marker project deployment guidance.",
        memory_type="procedure",
        workspace="work",
        project="payment",
    )
    repo_memory = bus.put(
        content="specificity-marker payment-api deployment guidance.",
        memory_type="procedure",
        workspace="work",
        project="payment",
        repo="payment-api",
    )

    results = bus.search(
        "specificity-marker",
        workspace="work",
        project="payment",
        repo="payment-api",
    )

    assert results[0].id == repo_memory.id
    assert project_memory.id in {memory.id for memory in results}


def test_exact_identifier_is_retrievable(bus: MemBus) -> None:
    memory = bus.put(
        title="BUG-18472",
        content="BUG-18472 was caused by reconcilePayment retrying after a partial settlement.",
        memory_type="incident",
        project="payment",
    )

    assert bus.search("BUG-18472", project="payment")[0].id == memory.id


def test_alias_expansion_recovers_fts_match(bus: MemBus) -> None:
    memory = bus.put(
        content="PostgreSQL connection pool was exhausted by worker concurrency.",
        memory_type="incident",
        project="payment",
    )
    bus.put_alias("pg", "postgresql")

    assert bus.search("pg", project="payment")[0].id == memory.id


def test_delete_removes_fts_row(bus: MemBus) -> None:
    memory = bus.put(content="temporary-marker", memory_type="note")
    assert bus.delete(memory.id) is True
    assert bus.search("temporary-marker") == []

    with bus._connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE memory_id = ?",
            (memory.id,),
        ).fetchone()[0] == 0


def test_backup_restores_data(tmp_path: Path) -> None:
    bus = MemBus(tmp_path / "source.db")
    memory = bus.put(
        content="This memory must survive a SQLite backup.",
        memory_type="fact",
    )

    bus.backup(tmp_path / "backup.db")

    assert MemBus(tmp_path / "backup.db").get(memory.id) == memory


def test_branch_requires_repo(bus: MemBus) -> None:
    with pytest.raises(ValueError, match="branch requires repo"):
        bus.put(
            content="invalid branch context",
            memory_type="fact",
            branch="main",
        )


def test_type_filter(bus: MemBus) -> None:
    procedure = bus.put(
        content="Run migrations before deployment.",
        memory_type="procedure",
    )
    bus.put(
        content="Deployment failed yesterday.",
        memory_type="incident",
    )

    results = bus.search("deployment", types=("procedure",))

    assert [memory.id for memory in results] == [procedure.id]


def test_search_limit_is_bounded(bus: MemBus) -> None:
    with pytest.raises(ValueError, match="limit"):
        bus.search("anything", limit=101)


def test_concurrent_updates_preserve_independent_changes(
    bus: MemBus, monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = bus.put(
        title="Original title",
        content="Original content",
        memory_type="note",
    )

    # On the previous implementation both updates read the same stale row
    # before either starts a write transaction. The fixed implementation
    # reads only after BEGIN IMMEDIATE, so this hook is never invoked.
    original_get = bus.get
    reads_completed = Barrier(2)

    def synchronized_get(memory_id: str):
        result = original_get(memory_id)
        reads_completed.wait(timeout=10)
        return result

    monkeypatch.setattr(bus, "get", synchronized_get)

    with ThreadPoolExecutor(max_workers=2) as executor:
        update_title = executor.submit(bus.update, memory.id, title="New title")
        update_content = executor.submit(
            bus.update, memory.id, content="New content"
        )
        update_title.result(timeout=15)
        update_content.result(timeout=15)

    final = original_get(memory.id)
    assert final is not None
    assert final.title == "New title"
    assert final.content == "New content"


def test_delete_between_old_read_and_write_cannot_report_ghost_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "race.db"
    updater = MemBus(path)
    deleter = MemBus(path)
    memory = updater.put(content="Before deletion", memory_type="note")

    # Pause only the updater before it enters its write transaction.
    # Previously update() already read the row at this point. Now the
    # initial SELECT occurs inside this transaction after the deletion.
    original_transaction = updater._transaction
    update_ready = Event()
    continue_update = Event()

    @contextmanager
    def delayed_transaction():
        update_ready.set()
        if not continue_update.wait(timeout=10):
            raise TimeoutError("update transaction was not released")
        with original_transaction() as conn:
            yield conn

    monkeypatch.setattr(updater, "_transaction", delayed_transaction)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            updater.update, memory.id, content="Should not be saved"
        )
        try:
            assert update_ready.wait(timeout=10)
            assert deleter.delete(memory.id) is True
        finally:
            continue_update.set()

        with pytest.raises(KeyError, match=memory.id):
            future.result(timeout=15)

    assert deleter.get(memory.id) is None
