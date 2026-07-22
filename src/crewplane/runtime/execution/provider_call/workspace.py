from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from crewplane.architecture.contracts import InvocationContext
from crewplane.runtime.workspace import (
    PreparedWorkspace,
    WorkspaceInvocationRequest,
    prepare_invocation_workspace,
)
from crewplane.runtime.workspace.cleanup_notes import note_cleanup_failure
from crewplane.runtime.workspace.setup import WorkspaceSetupCancellation

from ..runtime_context import DeferredAsyncCleanupRegistry

PREPARATION_CANCELLATION_TIMEOUT_SECONDS = 0.5
PREPARATION_CANCELLATION_MESSAGE = (
    "Provider invocation was cancelled during workspace preparation."
)


@dataclass(frozen=True)
class _WorkspacePreparationResult:
    prepared_workspace: PreparedWorkspace
    cancellation_finalized: bool


class _WorkspaceCancellationFinalizationError(RuntimeError):
    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


async def prepare_workspace_with_cancellation(
    workspace_request: WorkspaceInvocationRequest,
    invocation_context: InvocationContext,
    cleanup_registry: DeferredAsyncCleanupRegistry,
) -> PreparedWorkspace:
    setup_cancellation = WorkspaceSetupCancellation()
    prepare_task = asyncio.create_task(
        asyncio.to_thread(
            _prepare_workspace,
            workspace_request,
            invocation_context,
            setup_cancellation,
        )
    )
    try:
        preparation = await asyncio.shield(prepare_task)
    except asyncio.CancelledError as cancel:
        await _handle_preparation_cancellation(
            prepare_task,
            setup_cancellation,
            cancel,
            cleanup_registry,
        )
        raise
    if preparation.cancellation_finalized:
        raise RuntimeError("Workspace preparation finalized unexpected cancellation.")
    return preparation.prepared_workspace


def _prepare_workspace(
    workspace_request: WorkspaceInvocationRequest,
    invocation_context: InvocationContext,
    setup_cancellation: WorkspaceSetupCancellation,
) -> _WorkspacePreparationResult:
    prepared_workspace = prepare_invocation_workspace(
        replace(workspace_request, setup_cancellation=setup_cancellation),
        invocation_context,
    )
    if not setup_cancellation.is_cancelled():
        return _WorkspacePreparationResult(prepared_workspace, False)
    try:
        _mark_prepared_workspace_cancelled_now(prepared_workspace)
    except Exception as error:
        raise _WorkspaceCancellationFinalizationError(error) from error
    return _WorkspacePreparationResult(prepared_workspace, True)


async def _handle_preparation_cancellation(
    prepare_task: asyncio.Task[_WorkspacePreparationResult],
    setup_cancellation: WorkspaceSetupCancellation,
    cancel: asyncio.CancelledError,
    cleanup_registry: DeferredAsyncCleanupRegistry,
) -> None:
    await asyncio.to_thread(setup_cancellation.cancel)
    try:
        preparation = await asyncio.wait_for(
            asyncio.shield(prepare_task),
            PREPARATION_CANCELLATION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        _schedule_preparation_cancellation_cleanup(prepare_task, cleanup_registry)
        return
    except _WorkspaceCancellationFinalizationError as cleanup_error:
        note_cleanup_failure(
            cancel,
            "Workspace preparation cancellation handling",
            cleanup_error.error,
        )
        return
    except Exception as exc:
        raise cancel from exc
    if preparation.cancellation_finalized:
        return
    await _mark_prepared_workspace_cancelled(
        preparation.prepared_workspace,
        cleanup_registry,
        cancel,
    )


def _schedule_preparation_cancellation_cleanup(
    prepare_task: asyncio.Task[_WorkspacePreparationResult],
    cleanup_registry: DeferredAsyncCleanupRegistry,
) -> None:
    async def cleanup_when_ready() -> None:
        try:
            preparation = await asyncio.shield(prepare_task)
        except asyncio.CancelledError:
            return
        if preparation.cancellation_finalized:
            return
        await _mark_prepared_workspace_cancelled(
            preparation.prepared_workspace,
            cleanup_registry,
        )

    cleanup_registry.register(cleanup_when_ready(), cancel_on_timeout=False)


async def _mark_prepared_workspace_cancelled(
    prepared_workspace: PreparedWorkspace,
    cleanup_registry: DeferredAsyncCleanupRegistry,
    cancel: asyncio.CancelledError | None = None,
) -> None:
    mark_task = asyncio.create_task(
        asyncio.to_thread(
            _mark_prepared_workspace_cancelled_now,
            prepared_workspace,
        )
    )
    try:
        await asyncio.wait_for(
            asyncio.shield(mark_task),
            PREPARATION_CANCELLATION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        _schedule_mark_cancelled_completion(mark_task, cleanup_registry)
    except Exception as cleanup_error:
        if cancel is not None:
            note_cleanup_failure(
                cancel,
                "Workspace preparation cancellation handling",
                cleanup_error,
            )
            return
        raise


def _mark_prepared_workspace_cancelled_now(
    prepared_workspace: PreparedWorkspace,
) -> None:
    prepared_workspace.mark_cancelled(
        PREPARATION_CANCELLATION_MESSAGE,
        workspace_child_environment_applied(prepared_workspace, False),
    )


def _schedule_mark_cancelled_completion(
    mark_task: asyncio.Task[None],
    cleanup_registry: DeferredAsyncCleanupRegistry,
) -> None:
    async def cleanup_when_ready() -> None:
        await asyncio.shield(mark_task)

    cleanup_registry.register(cleanup_when_ready(), cancel_on_timeout=False)


def workspace_child_environment_applied(
    prepared_workspace: PreparedWorkspace,
    child_environment_applied: bool,
) -> bool | None:
    workspace = prepared_workspace.invocation_context.workspace
    if workspace is None or workspace.child_environment_required is not True:
        return None
    return child_environment_applied
