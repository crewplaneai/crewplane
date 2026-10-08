import asyncio
from types import SimpleNamespace

import pytest

from crewplane.cli.run.observability import await_workflow_or_stop_request
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
    unconfirmed_process_cleanup,
)


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
def test_cancellation_preserves_workflow_cleanup_failure(completed, wrapped) -> None:
    async def check() -> None:
        owner = asyncio.current_task()
        assert owner is not None
        cleanup = ProcessDrainError(
            ProcessDrainEvidence(123, None, True, False), "tree cleanup unresolved"
        )
        failure: BaseException = cleanup
        if wrapped:
            failure = asyncio.CancelledError()
            failure.__cause__ = cleanup

        async def workflow() -> None:
            if completed:
                asyncio.get_running_loop().call_soon(owner.cancel)
                raise failure
            owner.cancel()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise failure from failure.__cause__

        with pytest.raises(asyncio.CancelledError) as cancelled:
            await await_workflow_or_stop_request(
                workflow(), SimpleNamespace(stop_requested=False)
            )
        assert unconfirmed_process_cleanup(cancelled.value) is cleanup

    asyncio.run(check())


def test_cancellation_retrieves_completed_success() -> None:
    async def check() -> None:
        owner = asyncio.current_task()
        assert owner is not None

        async def workflow() -> None:
            asyncio.get_running_loop().call_soon(owner.cancel)

        with pytest.raises(asyncio.CancelledError) as cancelled:
            await await_workflow_or_stop_request(
                workflow(), SimpleNamespace(stop_requested=False)
            )
        assert unconfirmed_process_cleanup(cancelled.value) is None

    asyncio.run(check())
