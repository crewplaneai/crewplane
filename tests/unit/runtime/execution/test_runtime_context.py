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


def test_deferred_cleanup_registry_awaits_protected_tasks_after_timeout() -> None:
    async def run_test() -> tuple[tuple[Exception, ...], bool, int, int]:
        registry = DeferredAsyncCleanupRegistry()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def cleanup() -> None:
            await release.wait()
            finished.set()

        registry.register(cleanup(), False)
        drain_task = asyncio.create_task(registry.drain(0))
        await asyncio.sleep(0)
        drain_waited_for_cleanup = not drain_task.done()
        task_count_after_timeout = len(registry.tasks)
        release.set()
        errors = await asyncio.wait_for(drain_task, 1.0)
        return (
            errors,
            drain_waited_for_cleanup,
            task_count_after_timeout,
            len(registry.tasks),
        )

    errors, drain_waited, task_count_after_timeout, final_task_count = asyncio.run(
        run_test()
    )

    assert any(isinstance(error, TimeoutError) for error in errors)
    assert drain_waited is True
    assert task_count_after_timeout == 1
    assert final_task_count == 0


def test_deferred_cleanup_registry_returns_protected_task_errors() -> None:
    async def run_test() -> tuple[tuple[Exception, ...], bool]:
        registry = DeferredAsyncCleanupRegistry()
        release = asyncio.Event()

        async def cleanup() -> None:
            await release.wait()
            raise RuntimeError("late cleanup failure")

        registry.register(cleanup(), False)
        drain_task = asyncio.create_task(registry.drain(0))
        await asyncio.sleep(0)
        drain_waited_for_cleanup = not drain_task.done()
        release.set()
        errors = await asyncio.wait_for(drain_task, 1.0)
        return errors, drain_waited_for_cleanup

    errors, drain_waited = asyncio.run(run_test())

    assert any(isinstance(error, TimeoutError) for error in errors)
    assert any(str(error) == "late cleanup failure" for error in errors)
    assert drain_waited is True


def test_deferred_cleanup_registry_awaits_protected_follow_up_tasks() -> None:
    async def run_test() -> tuple[tuple[Exception, ...], bool]:
        registry = DeferredAsyncCleanupRegistry()
        release_initial = asyncio.Event()
        release_follow_up = asyncio.Event()
        follow_up_started = asyncio.Event()

        async def follow_up() -> None:
            follow_up_started.set()
            await release_follow_up.wait()

        async def initial_cleanup() -> None:
            await release_initial.wait()
            registry.register(follow_up(), cancel_on_timeout=False)

        registry.register(initial_cleanup(), cancel_on_timeout=False)
        drain_task = asyncio.create_task(registry.drain(0))
        await asyncio.sleep(0)
        release_initial.set()
        await asyncio.wait_for(follow_up_started.wait(), 1.0)
        drain_waited_for_follow_up = not drain_task.done()
        release_follow_up.set()
        errors = await asyncio.wait_for(drain_task, 1.0)
        return errors, drain_waited_for_follow_up

    errors, drain_waited = asyncio.run(run_test())

    assert any(isinstance(error, TimeoutError) for error in errors)
    assert drain_waited is True
