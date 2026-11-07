import asyncio
from contextvars import ContextVar
from pathlib import Path
from threading import get_ident
from unittest.mock import patch

import pytest

from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.memory.store import MemoryPublicationError, MemoryStore
from crewplane.core.memory.candidates import CandidateDocument, LessonProposal
from crewplane.core.memory.lifecycle import import_candidates
from crewplane.core.memory.models import MemorySnapshot, ProposalProvenance
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


@pytest.mark.parametrize("changed", [False, True])
def test_transaction_callback_context_result_and_publication_order(
    tmp_path: Path, monkeypatch, changed: bool
):
    context = ContextVar("transaction-owner", default="missing")
    result = object()
    events = []

    def publish(path, payload, ensure_parent=True):
        events.append("publish")
        return atomic_write_json(path, payload, ensure_parent)

    async def scenario():
        context.set("caller")
        loop = asyncio.get_running_loop()
        thread = get_ident()
        store = MemoryStore(tmp_path)
        initial = await store.initialize()
        original = store.path.read_bytes()
        inode = store.path.stat().st_ino

        def mutate(current):
            assert asyncio.get_running_loop() is loop
            assert get_ident() == thread
            assert context.get() == "caller"
            assert current == initial
            assert store.path.read_bytes() == original
            events.append("callback")
            updated = proposal(current)[0] if changed else current.model_copy()
            return updated, result

        monkeypatch.setattr(
            "crewplane.artifacts.memory.store.atomic_write_json", publish
        )
        assert await store.transact(mutate) is result
        assert events == (["callback", "publish"] if changed else ["callback"])
        snapshot = await store.snapshot()
        if changed:
            assert len(snapshot.records) == 1
            assert snapshot.transaction_id != initial.transaction_id
        else:
            assert snapshot == initial
            assert store.path.read_bytes() == original
            assert store.path.stat().st_ino == inode

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["callback", "validation"])
def test_transaction_failure_preserves_store_and_releases_lock(
    tmp_path: Path, failure: str
):
    error = ValueError("callback failed")

    async def scenario():
        store = MemoryStore(tmp_path)
        initial = await store.initialize()
        original = store.path.read_bytes()
        calls = []

        def mutate(current):
            calls.append(current)
            if failure == "callback":
                raise error
            return current.model_copy(update={"schema_version": "future"}), None

        with pytest.raises(ValueError) as captured:
            await store.transact(mutate)
        if failure == "callback":
            assert captured.value is error
        else:
            assert "Unsupported memory schema" in str(captured.value)
        assert calls == [initial]
        assert store.path.read_bytes() == original
        assert await store.snapshot() == initial
        assert len(await store.transact(proposal)) == 1

    asyncio.run(scenario())


def test_failed_initial_publication_reports_missing_readback(tmp_path: Path):
    error = OSError("initial publication failed")

    async def scenario():
        store = MemoryStore(tmp_path)
        with (
            patch(
                "crewplane.artifacts.memory.store.atomic_write_json_if_absent",
                side_effect=error,
            ),
            pytest.raises(MemoryPublicationError) as captured,
        ):
            await store.initialize()
        assert captured.value.transaction_id == MemorySnapshot().transaction_id
        assert captured.value.observed == "not_published"
        assert captured.value.__cause__ is error
        assert not store.path.exists()
        inode = store.lock_path.stat().st_ino
        assert await store.initialize() == MemorySnapshot()
        assert store.lock_path.stat().st_ino == inode

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
