import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.memory.store import MemoryPublicationError, MemoryStore
from crewplane.core.memory.candidates import CandidateDocument, LessonProposal
from crewplane.core.memory.lifecycle import import_candidates
from crewplane.core.memory.models import ProposalProvenance
from crewplane.version import SCHEMA_VERSION
from tests.helpers.platforms import requires_posix


def proposal(snapshot):
    return import_candidates(
        snapshot,
        CandidateDocument(
            schema_version=SCHEMA_VERSION,
            lessons=(LessonProposal(title="Lesson", body="Keep the whole lesson."),),
        ),
        ProposalProvenance(author="human", source_identity="manual"),
        {},
    )


def test_explicit_initialization_and_persistent_lock(tmp_path: Path):
    async def scenario():
        store = MemoryStore(tmp_path)
        with pytest.raises(ValueError, match="init"):
            await store.snapshot()
        assert not store.directory.exists()
        await store.initialize()
        lock_identity = store.lock_path.stat().st_ino
        identities = await store.transact(proposal)
        assert len(identities) == 1
        assert await store.initialize() == await store.snapshot()
        assert store.lock_path.stat().st_ino == lock_identity
        store.lock_path.unlink()
        with pytest.raises(ValueError, match="without.*lock"):
            await store.initialize()

    asyncio.run(scenario())


def test_interrupted_initialization_and_unsafe_store(tmp_path: Path):
    async def scenario():
        store = MemoryStore(tmp_path)
        store.directory.mkdir(parents=True)
        store.lock_path.touch()
        await store.initialize()
        original = store.path.read_bytes()
        store.path.unlink()
        target = tmp_path / "target"
        target.write_bytes(original)
        store.path.symlink_to(target)
        with pytest.raises(ValueError):
            await store.snapshot()
        assert target.read_bytes() == original

    asyncio.run(scenario())


def test_failed_publication_reports_readback_without_replaying(tmp_path: Path):
    async def scenario():
        store = MemoryStore(tmp_path)
        await store.initialize()

        def publish_then_fail(path, payload, ensure_parent=True):
            atomic_write_json(path, payload, ensure_parent)
            raise OSError("directory sync failed")

        with (
            patch(
                "crewplane.artifacts.memory.store.atomic_write_json",
                new=publish_then_fail,
            ),
            pytest.raises(MemoryPublicationError, match="published") as captured,
        ):
            await store.transact(proposal)
        assert captured.value.observed == "published"
        assert len((await store.snapshot()).records) == 1
        assert len(await store.transact(proposal)) == 1

    asyncio.run(scenario())


@pytest.mark.race
def test_waiting_lock_cancellation_does_not_leave_a_worker(tmp_path: Path):
    async def scenario():
        from crewplane.artifacts.memory.locking import memory_lock

        store = MemoryStore(tmp_path)
        await store.initialize()
        async with memory_lock(store.lock_path):
            waiter = asyncio.create_task(store.snapshot())
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        assert not (await store.snapshot()).records

    asyncio.run(scenario())


@requires_posix
@pytest.mark.race
def test_initialize_rechecks_concurrently_published_safe_lock(tmp_path, monkeypatch):
    from crewplane.architecture import safe_files_posix

    store = MemoryStore(tmp_path)
    original_lookup = safe_files_posix.contained_regular_file
    published = []

    def lookup_then_publish(root, parts):
        result = original_lookup(root, parts)
        if root / Path(*parts) == store.lock_path and result is None and not published:
            store.lock_path.write_bytes(b"")
            published.append(store.lock_path.stat().st_ino)
        return result

    monkeypatch.setattr(safe_files_posix, "contained_regular_file", lookup_then_publish)
    initialized = asyncio.run(store.initialize())
    assert initialized == asyncio.run(store.snapshot())
    assert published == [store.lock_path.stat().st_ino]
    assert store.lock_path.stat().st_nlink == 1
