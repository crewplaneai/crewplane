from __future__ import annotations

import asyncio

from orchestrator_cli.core.preflight.secrets import SecretContext
from orchestrator_cli.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
    DeferredAsyncCleanupRegistry,
)
from orchestrator_cli.version import SCHEMA_VERSION
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


def test_deferred_cleanup_registry_detaches_protected_tasks_after_timeout() -> None:
    async def run_test() -> tuple[tuple[Exception, ...], int, int]:
        registry = DeferredAsyncCleanupRegistry()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def cleanup() -> None:
            await release.wait()
            finished.set()

        registry.register(cleanup(), False)
        errors = await registry.drain(0.01)
        task_count_after_timeout = len(registry.tasks)
        release.set()
        await asyncio.wait_for(finished.wait(), 1.0)
        return errors, task_count_after_timeout, len(registry.tasks)

    errors, task_count_after_timeout, final_task_count = asyncio.run(run_test())

    assert any(isinstance(error, TimeoutError) for error in errors)
    assert task_count_after_timeout == 0
    assert final_task_count == 0


def test_deferred_cleanup_registry_reports_detached_task_errors() -> None:
    async def run_test() -> tuple[tuple[Exception, ...], list[object]]:
        registry = DeferredAsyncCleanupRegistry()
        loop = asyncio.get_running_loop()
        release = asyncio.Event()
        reported = asyncio.Event()
        handled_contexts: list[object] = []
        original_handler = loop.get_exception_handler()

        def handler(
            loop: asyncio.AbstractEventLoop, context: dict[str, object]
        ) -> None:
            del loop
            handled_contexts.append(context)
            reported.set()

        async def cleanup() -> None:
            await release.wait()
            raise RuntimeError("late cleanup failure")

        loop.set_exception_handler(handler)
        try:
            registry.register(cleanup(), False)
            errors = await registry.drain(0.01)
            release.set()
            await asyncio.wait_for(reported.wait(), 1.0)
        finally:
            loop.set_exception_handler(original_handler)
        return errors, handled_contexts

    errors, handled_contexts = asyncio.run(run_test())

    assert any(isinstance(error, TimeoutError) for error in errors)
    assert len(handled_contexts) == 1
    context = handled_contexts[0]
    assert isinstance(context, dict)
    assert (
        context["message"]
        == "Detached deferred cleanup task failed after drain timeout."
    )
    assert isinstance(context["exception"], RuntimeError)
    assert str(context["exception"]) == "late cleanup failure"
