from __future__ import annotations

import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from membus import MemoryCreate, MemoryService, SearchRequest, SQLiteStore


def _writer_worker(db_path: str, worker_id: int, count: int) -> int:
    service = MemoryService(
        SQLiteStore(
            db_path,
            busy_timeout_ms=10_000,
            begin_retry_attempts=8,
        )
    )
    for index in range(count):
        service.put(
            MemoryCreate(
                title=f"worker-{worker_id}-{index}",
                content=(
                    f"concurrency-marker worker-{worker_id} memory-{index} "
                    "tests SQLite WAL multi-process writes"
                ),
                memory_type="note",
            )
        )
    return count


def test_multiple_processes_can_write_one_canonical_store(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    MemoryService(SQLiteStore(path))

    worker_count = 4
    writes_per_worker = 12
    context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(
        max_workers=worker_count,
        mp_context=context,
    ) as executor:
        futures = [
            executor.submit(
                _writer_worker,
                str(path),
                worker,
                writes_per_worker,
            )
            for worker in range(worker_count)
        ]
        assert [future.result(timeout=30) for future in futures] == [
            writes_per_worker
        ] * worker_count

    service = MemoryService(SQLiteStore(path))
    results = service.search(SearchRequest(query="concurrency-marker", limit=100))
    assert len(results) == worker_count * writes_per_worker

    with service.store.connection() as conn:
        memory_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert memory_count == worker_count * writes_per_worker
