from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.artifacts.memory.locking import memory_lock
from crewplane.artifacts.memory.store import (
    MemoryPublicationError,
    MemoryStore,
    enforce_bounds,
)
from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.memory.models import MemorySnapshot
from tests.helpers.memory import accepted_snapshot, initialize_memory
from tests.helpers.platforms import requires_project_memory
from tests.unit.artifacts.memory.test_store import proposal

pytestmark = requires_project_memory


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
@pytest.mark.parametrize("filename", ["store.json", "store.lock"])
def test_unsafe_memory_files_are_rejected(tmp_path: Path, kind: str, filename: str):
    initialize_memory(tmp_path)
    store = MemoryStore(tmp_path)
    path = store.directory / filename
    target = tmp_path / "original"
    path.rename(target)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises((ValueError, OSError)):
        asyncio.run(store.snapshot())


def test_missing_schema_corruption_and_byte_bounds_are_explicit(tmp_path: Path):
    initialize_memory(tmp_path)
    store = MemoryStore(tmp_path)
    for payload in (
        b"not json",
        b"{}",
        json.dumps(
            {"schema_version": "future", "transaction_id": "x", "records": []}
        ).encode(),
        b" " * (16 * 1024 * 1024 + 1),
    ):
        store.path.write_bytes(payload)
        with pytest.raises(ValueError):
            asyncio.run(store.snapshot())
    snapshot = accepted_snapshot(LessonProposal(title="Large", body="x" * (32 * 1024)))
    with pytest.raises(ValueError, match="record.*limit"):
        enforce_bounds(snapshot)
    with (
        patch("crewplane.artifacts.memory.store.STORE_LIMIT_BYTES", 1),
        pytest.raises(ValueError, match="store exceeds"),
    ):
        enforce_bounds(MemorySnapshot())


@pytest.mark.parametrize("readback", ["not_published", "unknown"])
def test_publication_failure_readback_does_not_blindly_retry(tmp_path: Path, readback):
    initialize_memory(tmp_path)
    store = MemoryStore(tmp_path)
    before = store.path.read_bytes()

    def fail(path, payload, ensure_parent=True):
        del payload, ensure_parent
        if readback == "unknown":
            path.write_bytes(b"broken")
        raise OSError("replace failed")

    with (
        patch("crewplane.artifacts.memory.store.atomic_write_json", new=fail),
        pytest.raises(MemoryPublicationError) as captured,
    ):
        asyncio.run(store.transact(proposal))
    assert captured.value.observed == readback
    if readback == "not_published":
        assert store.path.read_bytes() == before


@pytest.mark.race
def test_bounded_wait_uses_same_persistent_inode(tmp_path: Path):
    initialize_memory(tmp_path)
    store = MemoryStore(tmp_path, lock_timeout_seconds=0.01)
    inode = store.lock_path.stat().st_ino

    async def scenario():
        async with memory_lock(store.lock_path):
            with pytest.raises(TimeoutError, match="Timed out"):
                await store.snapshot()
        assert not (await store.snapshot()).records

    asyncio.run(scenario())
    assert store.lock_path.stat().st_ino == inode


def test_store_does_not_publish_same_transaction_changed_state(tmp_path: Path):
    initialize_memory(tmp_path)
    store = MemoryStore(tmp_path)

    def invalid(current):
        return current.model_copy(
            update={
                "records": accepted_snapshot(
                    LessonProposal(title="A", body="B")
                ).records
            }
        ), None

    with pytest.raises(ValueError, match="new transaction"):
        asyncio.run(store.transact(invalid))
    assert not asyncio.run(store.snapshot()).records
