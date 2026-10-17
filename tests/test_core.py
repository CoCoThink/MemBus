from __future__ import annotations

from pathlib import Path

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


def test_context_mismatch_is_excluded(bus: MemBus) -> None:
    wrong = bus.put(
        content="PostgreSQL version for analytics is 15.",
        memory_type="fact",
        workspace="work",
        project="analytics",
    )
    right = bus.put(
        content="PostgreSQL version for payment is 17.",
        memory_type="fact",
        workspace="work",
        project="payment",
    )

    ids = [
        memory.id
        for memory in bus.search(
            "PostgreSQL version",
            workspace="work",
            project="payment",
        )
    ]

    assert right.id in ids
    assert wrong.id not in ids




def test_missing_workspace_does_not_match_workspace_specific_memory(bus: MemBus) -> None:
    global_memory = bus.put(
        content="context-containment-marker general payment guidance.",
        memory_type="note",
        project="payment",
    )
    bus.put(
        content="context-containment-marker personal payment guidance.",
        memory_type="note",
        workspace="personal",
        project="payment",
    )

    results = bus.search(
        "context-containment-marker",
        project="payment",
    )

    assert [memory.id for memory in results] == [global_memory.id]


def test_missing_repo_does_not_match_repo_specific_memory(bus: MemBus) -> None:
    project_memory = bus.put(
        content="repo-containment-marker project deployment guidance.",
        memory_type="procedure",
        workspace="work",
        project="payment",
    )
    bus.put(
        content="repo-containment-marker payment-api deployment guidance.",
        memory_type="procedure",
        workspace="work",
        project="payment",
        repo="payment-api",
    )

    results = bus.search(
        "repo-containment-marker",
        workspace="work",
        project="payment",
    )

    assert [memory.id for memory in results] == [project_memory.id]


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
