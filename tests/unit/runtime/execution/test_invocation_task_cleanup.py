import asyncio
from functools import partial

import pytest

from crewplane.architecture.contracts.invocation_failures import (
    InvocationFailureError,
    InvocationFailureSummary,
)
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
    unconfirmed_process_cleanup,
)
from crewplane.runtime.execution.errors import is_expected_execution_failure
from crewplane.runtime.execution.invocation_tasks import gather_invocations
from scripts.validation_policy import load_policy

TIMEOUTS = load_policy().timeouts


@pytest.mark.race
def test_cancellation_preserves_each_invocation_cleanup_failure():
    async def check():
        ready = asyncio.Event()
        closed = asyncio.Event()
        failure = ProcessDrainError(
            ProcessDrainEvidence(123, None, True, False), "unconfirmed tree"
        )

        async def invocation():
            ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                exc.__cause__ = failure
                raise
            finally:
                closed.set()

        task = asyncio.create_task(gather_invocations([invocation]))
        try:
            async with asyncio.timeout(TIMEOUTS.scenario_seconds) as scenario:
                await asyncio.wait_for(ready.wait(), TIMEOUTS.readiness_seconds)
                task.cancel()
                with pytest.raises(asyncio.CancelledError) as result:
                    await task
            assert not scenario.expired(), "scenario watchdog expired"
            assert unconfirmed_process_cleanup(result.value) is failure
        finally:
            task.cancel()
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), TIMEOUTS.cleanup_seconds
            )
            assert closed.is_set()
            assert task.done()

    asyncio.run(check())


@pytest.mark.race
def test_completed_failure_is_retained_when_sibling_is_cancelled():
    async def check():
        ready = asyncio.Event()
        closed = asyncio.Event()
        failure = ProcessDrainError(
            ProcessDrainEvidence(123, None, False, False), "unfinished"
        )

        failed = asyncio.Event()

        async def invocation(fail):
            if fail:
                try:
                    raise failure
                finally:
                    failed.set()
            await failed.wait()
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        task = asyncio.create_task(
            gather_invocations([partial(invocation, True), partial(invocation, False)])
        )
        try:
            async with asyncio.timeout(TIMEOUTS.scenario_seconds) as scenario:
                await asyncio.wait_for(ready.wait(), TIMEOUTS.readiness_seconds)
                task.cancel()
                with pytest.raises(asyncio.CancelledError) as result:
                    await task
            assert not scenario.expired(), "scenario watchdog expired"
            assert unconfirmed_process_cleanup(result.value) is failure
        finally:
            task.cancel()
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), TIMEOUTS.cleanup_seconds
            )
            assert closed.is_set()
            assert task.done()

    asyncio.run(check())


def test_gather_returns_results_in_input_order():
    async def first():
        await asyncio.sleep(0)
        return "first"

    async def second():
        raise ValueError("second")

    results = asyncio.run(gather_invocations([first, second]))
    assert results[0] == "first"
    assert isinstance(results[1], ValueError)


def test_wrapped_cleanup_failure_cannot_be_an_expected_provider_failure():
    error = InvocationFailureError(
        "provider failed",
        InvocationFailureSummary(
            "provider_error", "unknown", "none", "failed", "", False
        ),
        None,
    )
    assert is_expected_execution_failure(error)
    error.__cause__ = ProcessDrainError(
        ProcessDrainEvidence(123, None, True, False), "tree not empty"
    )
    assert not is_expected_execution_failure(error)
