from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from crewplane.artifacts.memory.store import MemoryStore
from tests.helpers.memory import initialize_memory
from tests.helpers.memory_processes import line, release, worker
from tests.helpers.platforms import requires_lock_recovery


@requires_lock_recovery
@pytest.mark.race
def test_real_process_writers_preserve_unrelated_updates_and_snapshots(tmp_path: Path):
    initialize_memory(tmp_path)
    store = MemoryStore(tmp_path)
    inode = store.lock_path.stat().st_ino
    with worker(tmp_path, "append", "first") as first:
        assert line(first) == "ready"
        assert line(first) == "attempting"
        assert line(first) == "locked"
        with worker(tmp_path, "append", "second") as second:
            assert line(second) == "ready"
            assert line(second) == "attempting"
            release(first)
            assert line(first) == "done"
            assert line(second) == "locked"
            release(second)
            assert line(second) == "done"
    snapshot = asyncio.run(store.snapshot())
    assert {record.revisions[0].title for record in snapshot.records} == {
        "first",
        "second",
    }
    assert store.lock_path.stat().st_ino == inode


@requires_lock_recovery
@pytest.mark.race
def test_crash_releases_flock_without_replacing_lock_inode(tmp_path: Path):
    initialize_memory(tmp_path)
    store = MemoryStore(tmp_path)
    inode = store.lock_path.stat().st_ino
    with worker(tmp_path, "append", "unpublished") as process:
        assert line(process) == "ready"
        assert line(process) == "attempting"
        assert line(process) == "locked"
        process.kill()
        process.wait(timeout=5)
    assert not asyncio.run(store.snapshot()).records
    assert store.lock_path.stat().st_ino == inode


@requires_lock_recovery
@pytest.mark.race
def test_concurrent_initialization_publishes_one_valid_store(tmp_path: Path):
    with (
        worker(tmp_path, "initialize", "first") as first,
        worker(tmp_path, "initialize", "second") as second,
    ):
        assert line(first) == line(second) == "ready"
        release(first)
        release(second)
        assert line(first) == line(second) == "done"
    store = MemoryStore(tmp_path)
    assert not asyncio.run(store.snapshot()).records
    inode = store.lock_path.stat().st_ino
    asyncio.run(store.initialize())
    assert store.lock_path.stat().st_ino == inode
