from __future__ import annotations

from pathlib import Path

import pytest

from membus import MemoryCreate, MemoryService, SearchContext, SearchRequest, SQLiteStore


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    return MemoryService(SQLiteStore(tmp_path / "memory.db"))


def test_crud(service: MemoryService) -> None:
    memory = service.put(
        MemoryCreate(
            title="Generated client rule",
            content="generated/client.ts is generated from OpenAPI and must not be edited.",
            memory_type="procedure",
            project="payment",
            repo="payment-api",
            source_type="user_rule",
        )
    )

    loaded = service.get(memory.id)
    assert loaded is not None
    assert loaded.content.startswith("generated/client.ts")

    updated = service.update(
        memory.id,
        content="generated/client.ts is generated from OpenAPI. Never edit it manually.",
    )
    assert "Never edit" in updated.content

    assert service.delete(memory.id) is True
    assert service.get(memory.id) is None
    assert service.delete(memory.id) is False


def test_context_mismatch_is_excluded(service: MemoryService) -> None:
    wrong = service.put(
        MemoryCreate(
            content="PostgreSQL version for analytics is 15.",
            memory_type="fact",
            workspace="work",
            project="analytics",
        )
    )
    right = service.put(
        MemoryCreate(
            content="PostgreSQL version for payment is 17.",
            memory_type="fact",
            workspace="work",
            project="payment",
        )
    )

    results = service.search(
        SearchRequest(
            query="PostgreSQL version",
            context=SearchContext(workspace="work", project="payment"),
        )
    )

    ids = [result.memory.id for result in results]
    assert right.id in ids
    assert wrong.id not in ids


def test_exact_identifier_is_first_class(service: MemoryService) -> None:
    memory = service.put(
        MemoryCreate(
            title="BUG-18472",
            content="BUG-18472 was caused by reconcilePayment retrying after a partial settlement.",
            memory_type="incident",
            project="payment",
        )
    )

    results = service.search(
        SearchRequest(
            query="BUG-18472",
            context=SearchContext(project="payment"),
        )
    )

    assert results[0].memory.id == memory.id
    assert results[0].match == "exact"


def test_alias_expansion_recovers_fts_match(service: MemoryService) -> None:
    memory = service.put(
        MemoryCreate(
            content="PostgreSQL connection pool was exhausted by worker concurrency.",
            memory_type="incident",
            project="payment",
        )
    )
    service.put_alias("pg", "postgresql")

    results = service.search(
        SearchRequest(
            query="pg",
            context=SearchContext(project="payment"),
        )
    )

    assert results[0].memory.id == memory.id
    assert results[0].match == "fts"


def test_delete_removes_memory_and_fts_row(service: MemoryService) -> None:
    memory = service.put(
        MemoryCreate(
            content="temporary-marker",
            memory_type="note",
        )
    )

    assert service.delete(memory.id) is True
    assert service.search(SearchRequest(query="temporary-marker")) == []

    with service.store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE memory_id = ?",
            (memory.id,),
        ).fetchone()[0] == 0


def test_backup_restores_canonical_data(tmp_path: Path) -> None:
    source = SQLiteStore(tmp_path / "source.db")
    service = MemoryService(source)
    memory = service.put(
        MemoryCreate(
            content="This memory must survive a SQLite backup.",
            memory_type="fact",
        )
    )

    source.backup(tmp_path / "backup.db")

    restored = MemoryService(SQLiteStore(tmp_path / "backup.db"))
    assert restored.get(memory.id) is not None


def test_branch_requires_repo(service: MemoryService) -> None:
    with pytest.raises(ValueError, match="branch requires repo"):
        service.put(
            MemoryCreate(
                content="invalid branch context",
                memory_type="fact",
                branch="main",
            )
        )


def test_same_project_different_workspace_is_excluded(
    service: MemoryService,
) -> None:
    wrong = service.put(
        MemoryCreate(
            content="Payment project in personal workspace uses SQLite.",
            memory_type="fact",
            workspace="personal",
            project="payment",
        )
    )
    right = service.put(
        MemoryCreate(
            content="Payment project in work workspace uses PostgreSQL.",
            memory_type="fact",
            workspace="work",
            project="payment",
        )
    )

    results = service.search(
        SearchRequest(
            query="Payment project",
            context=SearchContext(workspace="work", project="payment"),
        )
    )

    ids = [result.memory.id for result in results]
    assert right.id in ids
    assert wrong.id not in ids


def test_search_accepts_public_maximum_limit(service: MemoryService) -> None:
    service.put(MemoryCreate(content="PostgreSQL connection pool", memory_type="fact"))
    assert len(service.search(SearchRequest(query="PostgreSQL", limit=100))) == 1
