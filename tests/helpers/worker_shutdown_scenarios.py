"""Subprocess scenarios that deliberately leave workspace workers blocked."""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
from dataclasses import dataclass, field
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from crewplane.architecture.contracts import InvocationContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invocation import retry_reset
from crewplane.runtime.execution.deferred_cleanup import DeferredAsyncCleanupRegistry
from crewplane.runtime.execution.provider_call import generated_files
from crewplane.runtime.execution.provider_call.cancellation import (
    WorkspaceFinalizationDeferredCancellation,
)

SHUTDOWN_TIMEOUT_SECONDS = 10.0
WORKER_DEADLINE_SECONDS = 0.01


@dataclass
class BlockingWorker:
    started: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)

    def block(self, *args: object) -> None:
        del args
        self.started.set()
        self.release.wait()

    async def wait_until_started(self) -> None:
        while not self.started.is_set():
            await asyncio.sleep(0)


async def workspace_finalization() -> None:
    registry = DeferredAsyncCleanupRegistry()
    worker = BlockingWorker()
    request = SimpleNamespace(
        runtime_context=SimpleNamespace(deferred_workspace_cleanups=registry)
    )
    workspace = SimpleNamespace(state_path=None, mark_succeeded=worker.block)
    with patch.object(
        generated_files,
        "WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS",
        WORKER_DEADLINE_SECONDS,
    ):
        finalization = asyncio.create_task(
            generated_files.finalize_successful_workspace(
                request, workspace, None, None
            )
        )
        await worker.wait_until_started()
        finalization.cancel()
        with pytest.raises(WorkspaceFinalizationDeferredCancellation):
            await finalization
        errors = await registry.drain(WORKER_DEADLINE_SECONDS)
    assert any(isinstance(error, TimeoutError) for error in errors)
    assert registry.has_unfinished_protected_tasks


async def retry_reset_timeout() -> None:
    reset_worker = BlockingWorker()
    cancellation_worker = BlockingWorker()
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="provider",
        role=ProviderRole.EXECUTOR,
        retry_reset=reset_worker.block,
        retry_reset_canceller=cancellation_worker.block,
    )
    with (
        patch.object(
            retry_reset, "RETRY_RESET_DEADLINE_SECONDS", WORKER_DEADLINE_SECONDS
        ),
        pytest.raises(
            RuntimeError,
            match=r"^Workspace retry reset exceeded its internal deadline\.$",
        ) as raised,
    ):
        await retry_reset.reset_before_retry(context)
    assert isinstance(raised.value.__cause__, TimeoutError)
    await reset_worker.wait_until_started()
    await cancellation_worker.wait_until_started()


def main() -> None:
    scenarios = {
        "workspace": workspace_finalization,
        "retry_reset": retry_reset_timeout,
    }
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=scenarios)
    args = parser.parse_args()

    # Exclude cold imports, but keep the watchdog armed through interpreter exit.
    faulthandler.dump_traceback_later(SHUTDOWN_TIMEOUT_SECONDS, exit=True)
    asyncio.run(scenarios[args.scenario]())
    print("asyncio-run-returned", flush=True)


if __name__ == "__main__":
    main()
