from __future__ import annotations

import asyncio

from crewplane.architecture.ports import ArtifactStorePort

from ...workspace.worktree.cache import WorktreeReuseCleanupResult
from ..common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    NodeStatus,
    safe_error_message,
)
from ..errors import WorkflowExecutionError
from ..workspace_files.generated import GeneratedFileWorkspaceCleanupResult
from .cleanup import (
    emit_cleanup_errors,
    refresh_workspace_node_manifests,
    refresh_workspace_node_manifests_for_state_paths,
)
from .execution_session import WorkflowExecutionSession

DEFERRED_WORKSPACE_CLEANUP_DRAIN_TIMEOUT_SECONDS = 30.0


async def _cancel_running_node_tasks(session: WorkflowExecutionSession) -> None:
    remaining_tasks = list(session.state.running.values())
    if not remaining_tasks:
        return
    for task in remaining_tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*remaining_tasks, return_exceptions=True)


async def collect_workflow_postconditions(
    session: WorkflowExecutionSession,
    output: ArtifactStorePort,
) -> list[Exception]:
    postcondition_errors: list[Exception] = []
    runtime_context = session.runtime_context
    try:
        await _cancel_running_node_tasks(session)
        postcondition_errors.extend(
            await _collect_deferred_workspace_cleanup_errors(
                runtime_context=runtime_context,
                telemetry=session.telemetry,
            )
        )
        generated_file_cleanup = await _collect_generated_file_workspace_cleanup(
            runtime_context=runtime_context,
            telemetry=session.telemetry,
        )
        postcondition_errors.extend(generated_file_cleanup.errors)

        worktree_cleanup = await _collect_worktree_reuse_cache_cleanup(
            runtime_context=runtime_context,
            telemetry=session.telemetry,
        )
        postcondition_errors.extend(worktree_cleanup.errors)

        postcondition_errors.extend(
            await _collect_descriptor_refresh_errors(
                session=session,
                output=output,
                worktree_cleanup=worktree_cleanup,
                generated_file_cleanup=generated_file_cleanup,
            )
        )
    finally:
        runtime_context.runtime_publications.close()

    return postcondition_errors


async def _collect_deferred_workspace_cleanup_errors(
    runtime_context: CompiledRuntimeContext,
    telemetry: ExecutionTelemetry | None,
) -> tuple[Exception, ...]:
    errors = await runtime_context.deferred_workspace_cleanups.drain(
        DEFERRED_WORKSPACE_CLEANUP_DRAIN_TIMEOUT_SECONDS
    )
    emit_cleanup_errors(
        telemetry,
        "workspace_preparation_cancellation_cleanup",
        errors,
    )
    return errors


async def _collect_generated_file_workspace_cleanup(
    runtime_context: CompiledRuntimeContext,
    telemetry: ExecutionTelemetry | None,
) -> GeneratedFileWorkspaceCleanupResult:
    generated_file_cleanup = await asyncio.to_thread(
        runtime_context.generated_file_workspaces.cleanup_all
    )
    emit_cleanup_errors(
        telemetry,
        "generated_file_workspace_cleanup",
        generated_file_cleanup.errors,
    )
    return generated_file_cleanup


async def _collect_worktree_reuse_cache_cleanup(
    runtime_context: CompiledRuntimeContext,
    telemetry: ExecutionTelemetry | None,
) -> WorktreeReuseCleanupResult:
    worktree_cleanup = await asyncio.to_thread(
        runtime_context.worktree_reuse_cache.cleanup_all
    )
    emit_cleanup_errors(telemetry, "worktree_reuse_cleanup", worktree_cleanup.errors)
    return worktree_cleanup


async def _collect_descriptor_refresh_errors(
    session: WorkflowExecutionSession,
    output: ArtifactStorePort,
    worktree_cleanup: WorktreeReuseCleanupResult,
    generated_file_cleanup: GeneratedFileWorkspaceCleanupResult,
) -> list[Exception]:
    plan = session.runtime_context.plan
    statuses: dict[str, NodeStatus] = session.state.statuses
    postcondition_errors: list[Exception] = []
    state_refresh_failures = await refresh_workspace_node_manifests_for_state_paths(
        plan,
        output,
        statuses,
        worktree_cleanup.updated_state_paths,
        session.telemetry,
    )
    descriptor_refresh_failures = await refresh_workspace_node_manifests(
        plan,
        output,
        statuses,
        set(generated_file_cleanup.cleaned_node_ids),
        session.telemetry,
    )
    postcondition_errors.extend(exc for _, exc in state_refresh_failures)
    postcondition_errors.extend(exc for _, exc in descriptor_refresh_failures)
    return postcondition_errors


def raise_if_postcondition_errors(
    scheduler_succeeded: bool,
    postcondition_errors: list[Exception],
) -> None:
    if scheduler_succeeded and postcondition_errors:
        raise WorkflowExecutionError(
            "Workflow postconditions failed: "
            f"{len(postcondition_errors)} cleanup or descriptor error(s); "
            f"first error: {safe_error_message(postcondition_errors[0])}"
        )
