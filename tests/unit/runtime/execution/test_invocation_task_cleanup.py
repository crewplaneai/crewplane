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


def test_cancellation_preserves_each_invocation_cleanup_failure():
    async def check():
        ready = asyncio.Event()
        failure = ProcessDrainError(
            ProcessDrainEvidence(123, None, True, False), "unconfirmed tree"
        )

        async def invocation():
            ready.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError as exc:
                exc.__cause__ = failure
                raise

        task = asyncio.create_task(gather_invocations([invocation]))
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as result:
            await task
        assert unconfirmed_process_cleanup(result.value) is failure

    asyncio.run(check())


def test_completed_failure_is_retained_when_sibling_is_cancelled():
    async def check():
        ready = asyncio.Event()
        failure = ProcessDrainError(
            ProcessDrainEvidence(123, None, False, False), "unfinished"
        )

        async def invocation(fail):
            if fail:
                raise failure
            ready.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(
            gather_invocations([partial(invocation, True), partial(invocation, False)])
        )
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as result:
            await task
        assert unconfirmed_process_cleanup(result.value) is failure

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
