from __future__ import annotations

import asyncio

from crewplane.core.preflight.secrets import SecretContext
from crewplane.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
    DeferredAsyncCleanupRegistry,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_plan


def test_runtime_context_ignores_bool_concurrency_settings() -> None:
    plan = make_plan().model_copy(
        update={
            "runtime_config_snapshot": {
                "schema_version": SCHEMA_VERSION,
                "execution": {
                    "max_concurrent_nodes": True,
                    "max_parallel_invocations": False,
                },
            }
        }
    )
    context = CompiledRuntimeContext(plan=plan, secret_context=SecretContext())

    assert context.max_concurrent_nodes() is None
    assert context.max_parallel_invocations() is None


def test_deferred_cleanup_registry_returns_with_protected_task_still_running() -> None:
    async def run_test() -> tuple[tuple[Exception, ...], bool, int, bool]:
        registry = DeferredAsyncCleanupRegistry()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def cleanup() -> None:
            await release.wait()
            finished.set()

        registry.register(cleanup(), False)
        errors = await registry.drain(0)
        returned_before_cleanup = not finished.is_set()
        task_count_after_timeout = len(registry.tasks)
        release.set()
        await asyncio.wait_for(finished.wait(), 1.0)
        return (
            errors,
            returned_before_cleanup,
            task_count_after_timeout,
            finished.is_set(),
        )

    errors, returned_before_cleanup, task_count_after_timeout, finished = asyncio.run(
        run_test()
    )

    assert any(isinstance(error, TimeoutError) for error in errors)
    assert returned_before_cleanup is True
    assert task_count_after_timeout == 0
    assert finished is True


def test_deferred_cleanup_registry_does_not_wait_for_late_protected_errors() -> None:
    async def run_test() -> tuple[tuple[Exception, ...], bool]:
        registry = DeferredAsyncCleanupRegistry()
        release = asyncio.Event()
        failed = asyncio.Event()

        async def cleanup() -> None:
            await release.wait()
            failed.set()
            raise RuntimeError("late cleanup failure")

        registry.register(cleanup(), False)
        errors = await registry.drain(0)
        returned_before_failure = not failed.is_set()
        release.set()
        await asyncio.wait_for(failed.wait(), 1.0)
        await asyncio.sleep(0)
        return errors, returned_before_failure

    errors, returned_before_failure = asyncio.run(run_test())

    assert any(isinstance(error, TimeoutError) for error in errors)
    assert all(str(error) != "late cleanup failure" for error in errors)
    assert returned_before_failure is True


def test_deferred_cleanup_registry_tracks_protected_follow_up_after_timeout() -> None:
    async def run_test() -> tuple[tuple[Exception, ...], bool, bool, int, int]:
        registry = DeferredAsyncCleanupRegistry()
        release_initial = asyncio.Event()
        initial_finished = asyncio.Event()
        follow_up_started = asyncio.Event()
        release_follow_up = asyncio.Event()
        follow_up_finished = asyncio.Event()

        async def follow_up() -> None:
            follow_up_started.set()
            await release_follow_up.wait()
            follow_up_finished.set()

        async def initial_cleanup() -> None:
            await release_initial.wait()
            registry.register(follow_up(), cancel_on_timeout=False)
            initial_finished.set()

        registry.register(initial_cleanup(), cancel_on_timeout=False)
        errors = await registry.drain(0)
        release_initial.set()
        await asyncio.wait_for(initial_finished.wait(), 1.0)
        await asyncio.wait_for(follow_up_started.wait(), 1.0)
        while len(registry.protected_tasks) > 1:
            await asyncio.sleep(0)
        tracked_while_running = registry.has_unfinished_protected_tasks
        protected_task_count = len(registry.protected_tasks)
        task_count = len(registry.tasks)
        release_follow_up.set()
        await asyncio.wait_for(follow_up_finished.wait(), 1.0)
        await asyncio.sleep(0)
        assert not registry.has_unfinished_protected_tasks
        return (
            errors,
            follow_up_started.is_set(),
            tracked_while_running,
            protected_task_count,
            task_count,
        )

    errors, follow_up_started, tracked, protected_task_count, task_count = asyncio.run(
        run_test()
    )

    assert any(isinstance(error, TimeoutError) for error in errors)
    assert follow_up_started is True
    assert tracked is True
    assert protected_task_count == 1
    assert task_count == 0
