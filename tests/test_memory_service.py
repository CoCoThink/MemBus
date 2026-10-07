from __future__ import annotations

from pathlib import Path

import pytest

from membus import Database, MemoryCreate, MemoryService, SearchContext, SearchRequest
from membus.models import SearchManyRequest


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    db = Database(tmp_path / "memory.db")
    return MemoryService(db)


def test_crud_and_event_ledger(service: MemoryService) -> None:
    memory = service.put(
        MemoryCreate(
            title="Generated client rule",
            content="generated/client.ts is generated from OpenAPI and must not be edited.",
            memory_type="procedure",
            scope="repo",
            project="payment",
            repo="payment-api",
            importance=0.9,
            trust=1.0,
            source_type="user_rule",
        ),
        actor="test",
    )

    loaded = service.get(memory.id)
    assert loaded is not None
    assert loaded.content.startswith("generated/client.ts")

    updated = service.update(
        memory.id,
        content="generated/client.ts is generated from OpenAPI. Never edit it manually.",
        actor="test",
    )
    assert "Never edit" in updated.content

    events = service.repository.list_events(memory.id)
    assert [event["event_type"] for event in events] == ["created", "updated"]

    assert service.delete(memory.id, actor="test") is True
    assert service.get(memory.id) is None
    assert service.get(memory.id, include_deleted=True) is not None
    assert service.delete(memory.id, actor="test") is False

    events = service.repository.list_events(memory.id)
    assert [event["event_type"] for event in events] == [
        "created",
        "updated",
        "deleted",
    ]


def test_project_scope_mismatch_is_excluded(service: MemoryService) -> None:
    wrong = service.put(
        MemoryCreate(
            title="Database version",
            content="PostgreSQL version for the analytics project is 15.",
            memory_type="fact",
            scope="project",
            project="analytics",
        )
    )
    right = service.put(
        MemoryCreate(
            title="Database version",
            content="PostgreSQL version for the payment project is 17.",
            memory_type="fact",
            scope="project",
            project="payment",
        )
    )

    results = service.search(
        SearchRequest(
            query="PostgreSQL version",
            context=SearchContext(project="payment"),
            explain=True,
        )
    )

    ids = [result.memory.id for result in results]
    assert right.id in ids
    assert wrong.id not in ids
    assert results[0].signals["scope_reason"] == "project match"


def test_exact_technical_identifier_is_retrievable(service: MemoryService) -> None:
    memory = service.put(
        MemoryCreate(
            title="BUG-18472",
            content="BUG-18472 was caused by reconcilePayment retrying after a partial settlement.",
            memory_type="incident",
            scope="project",
            project="payment",
        )
    )

    results = service.search(
        SearchRequest(
            query="BUG-18472",
            context=SearchContext(project="payment"),
            explain=True,
        )
    )

    assert results
    assert results[0].memory.id == memory.id
    assert results[0].signals["exact_match"] is True
    assert results[0].signals["lexical"] == 1.0


def test_alias_expansion_recovers_lexical_match(service: MemoryService) -> None:
    memory = service.put(
        MemoryCreate(
            title="Database incident",
            content="PostgreSQL connection pool was exhausted by worker concurrency.",
            memory_type="incident",
            scope="project",
            project="payment",
        )
    )
    service.put_alias("pg", "postgresql")

    results = service.search(
        SearchRequest(
            query="pg",
            context=SearchContext(project="payment"),
            explain=True,
        )
    )

    assert results
    assert results[0].memory.id == memory.id
    assert "postgresql" in results[0].signals["alias_expansion"]["pg"]


def test_logical_delete_is_excluded_from_search(service: MemoryService) -> None:
    memory = service.put(
        MemoryCreate(
            title="Temporary incident",
            content="connection pool incident temporary-marker",
            memory_type="incident",
            scope="global",
        )
    )
    assert service.search(SearchRequest(query="temporary-marker"))

    service.delete(memory.id)

    assert service.search(SearchRequest(query="temporary-marker")) == []
    deleted_results = service.search(
        SearchRequest(query="temporary-marker", include_deleted=True)
    )
    assert deleted_results
    assert deleted_results[0].memory.id == memory.id


def test_multi_query_fuses_candidates(service: MemoryService) -> None:
    primary = service.put(
        MemoryCreate(
            title="Settlement concurrency",
            content=(
                "Settlement concurrency can exhaust the PostgreSQL connection pool "
                "when worker concurrency is increased."
            ),
            memory_type="incident",
            scope="project",
            project="payment",
        )
    )
    service.put(
        MemoryCreate(
            title="Unrelated PostgreSQL note",
            content="PostgreSQL backups run every night.",
            memory_type="note",
            scope="project",
            project="payment",
        )
    )

    results = service.search_many(
        SearchManyRequest.from_queries(
            ["settlement concurrency", "connection pool", "worker concurrency"],
            context=SearchContext(project="payment"),
            limit=5,
            explain=True,
        )
    )

    assert results
    assert results[0].memory.id == primary.id
    assert results[0].signals["query_hits"] >= 2


def test_backup_restores_canonical_data(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    backup_path = tmp_path / "backup.db"

    service = MemoryService(Database(source_path))
    memory = service.put(
        MemoryCreate(
            title="Backup test",
            content="This memory must survive a SQLite backup.",
            memory_type="fact",
            scope="global",
        )
    )

    service.db.backup(backup_path)

    restored = MemoryService(Database(backup_path))
    loaded = restored.get(memory.id)

    assert loaded is not None
    assert loaded.content == "This memory must survive a SQLite backup."


@pytest.mark.parametrize(
    ("scope", "kwargs"),
    [
        ("workspace", {}),
        ("project", {}),
        ("repo", {}),
        ("branch", {"repo": "payment-api"}),
    ],
)
def test_invalid_scope_is_rejected(
    service: MemoryService,
    scope: str,
    kwargs: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        service.put(
            MemoryCreate(
                content="invalid scope example",
                memory_type="fact",
                scope=scope,
                **kwargs,
            )
        )


def test_retrieval_logging_can_be_metadata_only(tmp_path: Path) -> None:
    service = MemoryService(
        Database(tmp_path / "memory.db"),
        retrieval_log_mode="metadata-only",
    )
    service.put(
        MemoryCreate(
            content="PostgreSQL connection pool",
            memory_type="fact",
            scope="global",
        )
    )

    service.search(SearchRequest(query="PostgreSQL"))

    with service.db.connection() as conn:
        row = conn.execute(
            "SELECT query, candidate_count, result_ids_json FROM retrieval_logs"
        ).fetchone()

    assert row is not None
    assert row["query"] is None
    assert row["candidate_count"] >= 1


def test_parent_workspace_mismatch_excludes_same_named_project(
    service: MemoryService,
) -> None:
    wrong = service.put(
        MemoryCreate(
            content="Payment project in personal workspace uses SQLite.",
            memory_type="fact",
            scope="project",
            workspace="personal",
            project="payment",
        )
    )
    right = service.put(
        MemoryCreate(
            content="Payment project in work workspace uses PostgreSQL.",
            memory_type="fact",
            scope="project",
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


def test_search_many_accepts_public_maximum_limit(service: MemoryService) -> None:
    service.put(
        MemoryCreate(
            content="PostgreSQL connection pool",
            memory_type="fact",
            scope="global",
        )
    )

    results = service.search_many(
        SearchManyRequest.from_queries(["PostgreSQL"], limit=100)
    )

    assert len(results) == 1
