from __future__ import annotations

import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event

import pytest

from membus import MemBus, NotFound, VersionConflict
from membus.doctor import run_doctor


def _writer_worker(db_path: str, worker_id: int, count: int) -> int:
    bus = MemBus(db_path)
    for index in range(count):
        bus.put(
            title=f"worker-{worker_id}-{index}",
            content=(
                f"concurrency-marker worker-{worker_id} memory-{index} "
                "tests SQLite WAL multi-process writes"
            ),
            memory_type="note",
        )
    return count


def _identical_writer(db_path: str, count: int) -> list[bool]:
    bus = MemBus(db_path)
    return [
        bus.put(content="retried identical write", memory_type="note").created
        for _ in range(count)
    ]


def _run_processes(fn, args_list, workers: int):
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as executor:
        futures = [executor.submit(fn, *args) for args in args_list]
        return [future.result(timeout=60) for future in futures]


def test_multiple_processes_can_write_one_store(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    MemBus(path)

    worker_count = 4
    writes_per_worker = 12
    results = _run_processes(
        _writer_worker,
        [(str(path), worker, writes_per_worker) for worker in range(worker_count)],
        worker_count,
    )
    assert results == [writes_per_worker] * worker_count

    bus = MemBus(path)
    assert len(bus.search("concurrency-marker", limit=100)) == (
        worker_count * writes_per_worker
    )
    with bus._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == (
            worker_count * writes_per_worker
        )
    assert run_doctor(bus)["ok"]


def test_parallel_identical_puts_converge_on_one_memory(tmp_path: Path) -> None:
    path = tmp_path / "dup.db"
    MemBus(path)

    outcomes = _run_processes(_identical_writer, [(str(path), 6)] * 4, 4)

    assert sum(created for run in outcomes for created in run) == 1
    bus = MemBus(path)
    assert len(bus.list().memories) == 1
    assert run_doctor(bus)["ok"]


def test_concurrent_updates_with_same_version_conflict(tmp_path: Path) -> None:
    bus = MemBus(tmp_path / "memory.db")
    memory = bus.put(title="Original", content="Original", memory_type="note").memory
    start = Barrier(2)

    def attempt(**changes):
        start.wait(timeout=10)
        try:
            return bus.update(memory.id, expected_version=1, **changes)
        except VersionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(attempt, title="Title by A")
        second = executor.submit(attempt, content="Content by B")
        outcomes = [first.result(timeout=15), second.result(timeout=15)]

    conflicts = [o for o in outcomes if isinstance(o, VersionConflict)]
    winners = [o for o in outcomes if not isinstance(o, VersionConflict)]
    assert len(conflicts) == 1 and len(winners) == 1
    # The loser sees the winner's state and can retry on top of it.
    assert conflicts[0].current.version == 2
    retried = bus.update(
        memory.id, expected_version=2,
        **({"content": "Content by B"} if winners[0].title == "Title by A"
           else {"title": "Title by A"}),
    )
    assert (retried.title, retried.content) == ("Title by A", "Content by B")


def test_delete_between_read_and_update_cannot_report_ghost_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "race.db"
    updater = MemBus(path)
    deleter = MemBus(path)
    memory = updater.put(content="Before deletion", memory_type="note").memory

    # Pause only the updater before it enters its write transaction; the
    # row is read inside that transaction, after the deletion.
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
            updater.update, memory.id, expected_version=1,
            content="Should not be saved",
        )
        try:
            assert update_ready.wait(timeout=10)
            assert deleter.delete(memory.id) is True
        finally:
            continue_update.set()

        with pytest.raises(NotFound):
            future.result(timeout=15)

    stored = deleter.get(memory.id)
    assert stored is not None and stored.content == "Before deletion"
