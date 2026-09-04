from __future__ import annotations

import asyncio
from dataclasses import dataclass

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
    cleanup_successful_workspace_run_refs,
    emit_cleanup_errors,
    refresh_workspace_node_manifests,
    refresh_workspace_node_manifests_for_state_paths,
)
from .execution_session import WorkflowExecutionSession

DEFERRED_WORKSPACE_CLEANUP_DRAIN_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class _WorkspaceCleanupPhaseResult:
    errors: tuple[Exception, ...]
    generated_files: GeneratedFileWorkspaceCleanupResult
    worktree_reuse: WorktreeReuseCleanupResult


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
    runtime_context = session.runtime_context
    try:
        await _cancel_running_node_tasks(session)
        cleanup_result = await _collect_workspace_cleanup_phases(
            runtime_context,
            session.telemetry,
        )
        postcondition_errors = list(cleanup_result.errors)
        postcondition_errors.extend(
            await _collect_descriptor_refresh_errors(
                session,
                output,
                cleanup_result.worktree_reuse,
                cleanup_result.generated_files,
            )
        )
        return postcondition_errors
    finally:
        _close_runtime_publications_if_safe(runtime_context)


async def _collect_workspace_cleanup_phases(
    runtime_context: CompiledRuntimeContext,
    telemetry: ExecutionTelemetry | None,
) -> _WorkspaceCleanupPhaseResult:
    deferred_errors = await _collect_deferred_workspace_cleanup_errors(
        runtime_context,
        telemetry,
    )
    errors = list(deferred_errors)
    cleanup_fenced = _workspace_cleanup_is_fenced(runtime_context)
    if not cleanup_fenced:
        errors.extend(
            await _collect_workspace_ref_cleanup_errors(runtime_context, telemetry)
        )
    if deferred_errors or cleanup_fenced:
        return _WorkspaceCleanupPhaseResult(
            tuple(errors),
            GeneratedFileWorkspaceCleanupResult(),
            WorktreeReuseCleanupResult(),
        )
    generated_files = await _collect_generated_file_workspace_cleanup(
        runtime_context,
        telemetry,
    )
    errors.extend(generated_files.errors)
    worktree_reuse = await _collect_worktree_reuse_cache_cleanup(
        runtime_context,
        telemetry,
    )
    errors.extend(worktree_reuse.errors)
    return _WorkspaceCleanupPhaseResult(
        tuple(errors),
        generated_files,
        worktree_reuse,
    )


async def _collect_workspace_ref_cleanup_errors(
    runtime_context: CompiledRuntimeContext,
    telemetry: ExecutionTelemetry | None,
) -> tuple[Exception, ...]:
    try:
        await cleanup_successful_workspace_run_refs(runtime_context.plan, telemetry)
    except Exception as exc:
        return (exc,)
    return ()


def _workspace_cleanup_is_fenced(runtime_context: CompiledRuntimeContext) -> bool:
    return getattr(
        runtime_context.deferred_workspace_cleanups,
        "has_unfinished_protected_tasks",
        False,
    )


def _close_runtime_publications_if_safe(
    runtime_context: CompiledRuntimeContext,
) -> None:
    if not _workspace_cleanup_is_fenced(runtime_context):
        runtime_context.runtime_publications.close()


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
        set(generated_file_cleanup.cleaned_node_ids)
        | {
            node.id
            for node in plan.nodes
            if node.workspace_policy is not None and node.workspace_policy.enabled
        },
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
