from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from crewplane.runtime.workspace import mutator_fence, state
from crewplane.runtime.workspace.cleanup_notes import note_cleanup_failure
from crewplane.runtime.workspace.snapshot import WorkspaceSnapshotCancelled

from ..deferred_cleanup import DeferredAsyncCleanupRegistry, workspace_worker_task


class WorkspaceWorkerFence:
    def __init__(self, state_path: Path | None, operation: str) -> None:
        self._state_path = state_path
        self._operation = operation
        self._lock = Lock()
        self._finished = False
        self._fenced = False
        self._durable = False

    def install_if_running(self) -> bool:
        with self._lock:
            if self._finished or self._state_path is None:
                return False
            mutator_fence.fence_workspace_mutator(self._state_path)
            self._fenced = True
            return True

    def persist(self) -> bool:
        with self._lock:
            if self._finished or not self._fenced or self._state_path is None:
                return False
            _persist_workspace_worker_state(self._state_path, self._operation)
            self._durable = True
            return True

    def finish(self) -> None:
        with self._lock:
            self._finished = True
            if not self._fenced or self._state_path is None:
                return
            if self._durable:
                _release_durable_workspace_worker_fence(
                    self._state_path,
                    self._operation,
                )
            else:
                mutator_fence.release_workspace_mutator(self._state_path)


@dataclass(frozen=True)
class WorkspaceWorkerCancellation:
    cleanup_registry: DeferredAsyncCleanupRegistry
    timeout_seconds: float

    async def wait_for_completion(
        self,
        task: asyncio.Task[Any],
        failure_context: str,
        cancel: asyncio.CancelledError,
        worker_fence: WorkspaceWorkerFence | None = None,
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                self.timeout_seconds,
            )
        except TimeoutError:
            self.defer_completion(task)
            if worker_fence is not None:
                await self.persist_fence_after_timeout(
                    worker_fence,
                    f"{failure_context} fence persistence",
                    cancel,
                )
        except WorkspaceSnapshotCancelled:
            return
        except Exception as exc:
            note_cleanup_failure(cancel, failure_context, exc)

    def defer_completion(self, task: asyncio.Task[Any]) -> None:
        self.cleanup_registry.register(_await_workspace_worker(task), False)

    async def persist_fence_after_timeout(
        self,
        worker_fence: WorkspaceWorkerFence,
        failure_context: str,
        cancel: asyncio.CancelledError,
    ) -> None:
        if not worker_fence.install_if_running():
            return
        fence_persistence = workspace_worker_task(worker_fence.persist)
        await self.wait_for_completion(
            fence_persistence,
            failure_context,
            cancel,
        )


def _persist_workspace_worker_state(state_path: Path, operation: str) -> None:
    state.mutate_workspace_state(
        state_path,
        lambda payload: payload.__setitem__(
            "workspace_mutator",
            {"status": "unresolved", "operation": operation},
        ),
    )


def _release_durable_workspace_worker_fence(
    state_path: Path,
    operation: str,
) -> None:
    try:
        state.mutate_workspace_state(
            state_path,
            lambda payload: payload.__setitem__(
                "workspace_mutator",
                {
                    "status": "confirmed",
                    "operation": operation,
                    "outcome": "finished",
                },
            ),
        )
    finally:
        mutator_fence.release_workspace_mutator(state_path)


async def _await_workspace_worker(task: asyncio.Task[Any]) -> None:
    try:
        await asyncio.shield(task)
    except WorkspaceSnapshotCancelled:
        return
