import asyncio
from contextvars import ContextVar
from pathlib import Path
from threading import Event, get_ident

import pytest

from crewplane.artifacts.memory import store as storage
from crewplane.artifacts.memory.locking import memory_lock
from scripts.validation_policy import load_policy
from tests.helpers.platforms import requires_project_memory
from tests.unit.artifacts.memory.test_store import proposal

pytestmark = requires_project_memory

TIMEOUTS = load_policy().timeouts


@pytest.mark.parametrize("action", ["initialize", "snapshot", "transact"])
def test_store_preparation_runs_outside_event_loop(tmp_path: Path, monkeypatch, action):
    async def scenario():
        store = storage.MemoryStore(tmp_path)
        if action != "initialize":
            await store.initialize()
        thread = get_ident()
        function = (
            "ensure_contained_directory"
            if action == "initialize"
            else "contained_directory"
        )
        original = getattr(storage, function)
        calls = []

        def prepare(*args, **kwargs):
            assert get_ident() != thread, "Store preparation blocked the event loop"
            calls.append(function)
            return original(*args, **kwargs)

        monkeypatch.setattr(storage, function, prepare)
        if action == "transact":
            assert len(await store.transact(proposal)) == 1
        else:
            assert not (await getattr(store, action)()).records
        assert calls == [function]

    asyncio.run(scenario())


@pytest.mark.race
@pytest.mark.parametrize(
    "action", ["initialize", "reinitialize", "snapshot", "transact_read", "transact"]
)
@pytest.mark.parametrize("storage_failure", [False, True])
def test_cancelled_store_io_finishes_before_unlocking(
    tmp_path: Path, monkeypatch, action: str, storage_failure: bool
):
    release = Event()
    completed = Event()
    error = OSError("store I/O failed")
    context = ContextVar("store-owner", default="missing")

    async def scenario():
        context.set("caller")
        store = storage.MemoryStore(tmp_path)
        if action != "initialize":
            await store.initialize()
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        thread = get_ident()
        function = {
            "initialize": "atomic_write_json_if_absent",
            "reinitialize": "read_contained_bytes",
            "snapshot": "read_contained_bytes",
            "transact_read": "read_contained_bytes",
            "transact": "atomic_write_json",
        }[action]
        original = getattr(storage, function)
        callbacks = []

        def blocked_io(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            assert get_ident() != thread, "Store I/O blocked the event loop"
            assert context.get() == "caller"
            try:
                assert release.wait(TIMEOUTS.scenario_seconds), "worker not released"
                result = original(*args, **kwargs)
                if storage_failure:
                    raise error
                return result
            finally:
                completed.set()

        def mutate(current):
            callbacks.append(current)
            return proposal(current)

        async def operate():
            if action in {"transact", "transact_read"}:
                return await store.transact(mutate)
            if action == "reinitialize":
                return await store.initialize()
            return await getattr(store, action)()

        with monkeypatch.context() as guarded:
            guarded.setattr(storage, function, blocked_io)
            task = asyncio.create_task(operate())
            try:
                async with asyncio.timeout(TIMEOUTS.scenario_seconds):
                    await asyncio.wait_for(entered.wait(), TIMEOUTS.readiness_seconds)
                    for message in ("first cancellation", "second cancellation"):
                        task.cancel(message)
                        await asyncio.sleep(0)
                        assert not task.done()
                        assert not completed.is_set()
                        with pytest.raises(TimeoutError):
                            async with memory_lock(store.lock_path, True, 0.01):
                                pytest.fail("Store I/O lost its lock")
                    release.set()
                    with pytest.raises(asyncio.CancelledError) as captured:
                        await task
                    if storage_failure:
                        cause = captured.value.__cause__
                        if action not in {"initialize", "transact"}:
                            assert cause is error
                        else:
                            assert isinstance(cause, storage.MemoryPublicationError)
                            assert cause.observed == "published"
                            assert cause.__cause__ is error
            finally:
                release.set()
                task.cancel()
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True),
                    TIMEOUTS.cleanup_seconds,
                )
        assert completed.is_set()
        assert len(callbacks) == (1 if action == "transact" else 0)
        snapshot = await store.snapshot()
        assert len(snapshot.records) == (1 if action == "transact" else 0)
        if action == "transact":
            assert len(await store.transact(proposal)) == 1

    asyncio.run(scenario())


@pytest.mark.race
def test_cancelled_publication_keeps_lock_through_protected_readback(
    tmp_path: Path, monkeypatch
):
    release = Event()
    completed = Event()
    error = OSError("directory sync failed")

    async def scenario():
        store = storage.MemoryStore(tmp_path)
        await store.initialize()
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        thread = get_ident()
        original_read = storage.read_contained_bytes
        original_write = storage.atomic_write_json
        reads = 0

        def publish_then_fail(*args, **kwargs):
            original_write(*args, **kwargs)
            raise error

        def read(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 1:
                return original_read(*args, **kwargs)
            loop.call_soon_threadsafe(entered.set)
            assert get_ident() != thread, "Protected readback blocked the event loop"
            try:
                assert release.wait(TIMEOUTS.scenario_seconds), "readback not released"
                return original_read(*args, **kwargs)
            finally:
                completed.set()

        with monkeypatch.context() as guarded:
            guarded.setattr(storage, "atomic_write_json", publish_then_fail)
            guarded.setattr(storage, "read_contained_bytes", read)
            task = asyncio.create_task(store.transact(proposal))
            try:
                async with asyncio.timeout(TIMEOUTS.scenario_seconds):
                    await asyncio.wait_for(entered.wait(), TIMEOUTS.readiness_seconds)
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done()
                    with pytest.raises(TimeoutError):
                        async with memory_lock(store.lock_path, True, 0.01):
                            pytest.fail("Publication readback lost its lock")
                    release.set()
                    with pytest.raises(asyncio.CancelledError) as captured:
                        await task
                    publication_error = captured.value.__cause__
                    assert isinstance(publication_error, storage.MemoryPublicationError)
                    assert publication_error.observed == "published"
                    assert publication_error.__cause__ is error
            finally:
                release.set()
                task.cancel()
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True),
                    TIMEOUTS.cleanup_seconds,
                )
        assert completed.is_set()
        snapshot = await store.snapshot()
        assert publication_error.transaction_id == snapshot.transaction_id
        assert len(snapshot.records) == 1

    asyncio.run(scenario())


@pytest.mark.race
@pytest.mark.parametrize("storage_failure", [False, True])
def test_runner_shutdown_finishes_store_io_before_owner_cleanup(
    tmp_path: Path, monkeypatch, storage_failure: bool
):
    release = Event()
    completed = Event()
    error = OSError("publication failed during shutdown")
    cleanup = []
    errors = []

    async def main():
        store = storage.MemoryStore(tmp_path)
        await store.initialize()
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        original = storage.atomic_write_json

        def publish(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(TIMEOUTS.scenario_seconds), "worker not released"
                original(*args, **kwargs)
                if storage_failure:
                    raise error
            finally:
                completed.set()

        monkeypatch.setattr(storage, "atomic_write_json", publish)

        async def owner():
            try:
                await store.transact(proposal)
            except (OSError, asyncio.CancelledError) as exc:
                errors.append(exc)
            finally:
                cleanup.append(completed.is_set())

        async def release_on_shutdown():
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                release.set()

        tasks = [
            asyncio.create_task(owner()),
            asyncio.create_task(release_on_shutdown()),
        ]
        await asyncio.wait_for(entered.wait(), TIMEOUTS.readiness_seconds)
        assert all(not task.done() for task in tasks)

    try:
        asyncio.run(main())
    finally:
        release.set()
    assert cleanup == [True]
    assert len(errors) == 1
    assert isinstance(errors[0], asyncio.CancelledError)
    if storage_failure:
        publication_error = errors[0].__cause__
        assert isinstance(publication_error, storage.MemoryPublicationError)
        assert publication_error.observed == "published"
        assert publication_error.__cause__ is error
    assert len(asyncio.run(storage.MemoryStore(tmp_path).snapshot()).records) == 1
