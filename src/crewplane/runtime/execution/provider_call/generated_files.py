from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from crewplane.artifacts.generated_files.catalog import (
    snapshot_generated_file_workspace,
)
from crewplane.runtime.workspace import PreparedWorkspace
from crewplane.runtime.workspace.cleanup_notes import note_cleanup_failure
from crewplane.runtime.workspace.state import RenderedWorkspaceFileDescriptor

from ..runtime_context import DeferredAsyncCleanupRegistry
from ..workspace_files import rendered_workspace_file_descriptor
from .generated_file_changes import (
    GeneratedFileChangeBaseline,
    changed_generated_file_paths,
    resolved_real_directory,
)
from .types import ProviderCallRequest, ProviderOutputPolicy

__all__ = (
    "GeneratedFileChangeBaseline",
    "capture_generated_file_change_baseline",
    "changed_generated_file_paths",
    "finalize_successful_workspace",
    "record_generated_file_workspace",
    "rendered_workspace_file_descriptors",
    "snapshot_invocation_generated_files",
    "snapshot_invocation_generated_files_async",
    "validated_generated_file_workspace_root",
)

WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS = 0.5


def rendered_workspace_file_descriptors(
    request: ProviderCallRequest,
) -> tuple[RenderedWorkspaceFileDescriptor, ...]:
    return tuple(
        rendered_workspace_file_descriptor(
            resolved_file,
            request.node_id,
            request.task_id,
            request.role_label,
            request.round_num,
            request.audit_round_num,
        )
        for resolved_file in request.rendered_workspace_files
    )


def record_generated_file_workspace(
    request: ProviderCallRequest,
    prepared_workspace: PreparedWorkspace,
    workspace_root: Path | None,
) -> None:
    cleanup = (
        prepared_workspace.cleanup_after_success
        if prepared_workspace.workspace_path is not None
        and prepared_workspace.cleanup_on_success
        else None
    )
    if workspace_root is None and cleanup is None:
        return
    request.runtime_context.generated_file_workspaces.record(
        request.node_id,
        request.output_file,
        workspace_root,
        cleanup,
    )


def capture_generated_file_change_baseline(
    prepared_workspace: PreparedWorkspace,
) -> GeneratedFileChangeBaseline | None:
    try:
        invocation_root = resolved_real_directory(
            prepared_workspace.cwd,
            "Invocation root",
        )
    except RuntimeError:
        return None
    return GeneratedFileChangeBaseline.capture(
        invocation_root,
        filesystem_fallback_enabled=prepared_workspace.workspace_path is not None,
    )


async def mark_workspace_succeeded(
    prepared_workspace: PreparedWorkspace,
    child_environment_applied: bool | None,
    cleanup_registry: DeferredAsyncCleanupRegistry,
) -> None:
    finalization = asyncio.create_task(
        asyncio.to_thread(
            prepared_workspace.mark_succeeded,
            child_environment_applied,
            True,
        )
    )
    try:
        await asyncio.shield(finalization)
    except asyncio.CancelledError as cancel:
        await _handle_cancelled_workspace_thread_task(
            finalization,
            cleanup_registry,
            "Workspace success finalization after cancellation",
            cancel,
        )
        raise


async def finalize_successful_workspace(
    request: ProviderCallRequest,
    prepared_workspace: PreparedWorkspace,
    child_environment_applied: bool | None,
    generated_file_workspace: Path | None,
) -> None:
    finalization = asyncio.create_task(
        asyncio.to_thread(
            prepared_workspace.mark_succeeded,
            child_environment_applied,
            True,
        )
    )
    try:
        await asyncio.shield(finalization)
    except asyncio.CancelledError as cancel:
        await _handle_cancelled_success_finalization(
            request,
            prepared_workspace,
            child_environment_applied,
            generated_file_workspace,
            finalization,
            cancel,
        )
        raise
    record_generated_file_workspace(
        request,
        prepared_workspace,
        generated_file_workspace,
    )


async def _handle_cancelled_success_finalization(
    request: ProviderCallRequest,
    prepared_workspace: PreparedWorkspace,
    child_environment_applied: bool | None,
    generated_file_workspace: Path | None,
    finalization: asyncio.Task[None],
    cancel: asyncio.CancelledError,
) -> None:
    try:
        await asyncio.wait_for(
            asyncio.shield(finalization),
            WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        cancel._crewplane_workspace_finalization_deferred = True  # type: ignore[attr-defined]
        request.runtime_context.deferred_workspace_cleanups.register(
            _record_generated_file_workspace_after_finalization(
                request,
                prepared_workspace,
                child_environment_applied,
                generated_file_workspace,
                finalization,
            ),
            False,
        )
    except Exception as exc:
        note_cleanup_failure(
            cancel,
            "Workspace success finalization after cancellation",
            exc,
        )
        await _mark_workspace_cancelled_after_finalization_failure(
            prepared_workspace,
            child_environment_applied,
            cancel,
        )
    else:
        record_generated_file_workspace(
            request,
            prepared_workspace,
            generated_file_workspace,
        )


async def _record_generated_file_workspace_after_finalization(
    request: ProviderCallRequest,
    prepared_workspace: PreparedWorkspace,
    child_environment_applied: bool | None,
    generated_file_workspace: Path | None,
    finalization: asyncio.Task[None],
) -> None:
    try:
        await asyncio.shield(finalization)
    except Exception as exc:
        await _mark_workspace_cancelled_after_finalization_failure(
            prepared_workspace,
            child_environment_applied,
            exc,
        )
        raise
    record_generated_file_workspace(
        request,
        prepared_workspace,
        generated_file_workspace,
    )
    cleanup_errors = (
        request.runtime_context.generated_file_workspaces.cleanup_node_best_effort(
            request.node_id
        )
    )
    if cleanup_errors:
        raise RuntimeError(
            "Generated-file workspace cleanup after deferred finalization failed "
            f"({len(cleanup_errors)} error(s))."
        ) from cleanup_errors[0]


async def _mark_workspace_cancelled_after_finalization_failure(
    prepared_workspace: PreparedWorkspace,
    child_environment_applied: bool | None,
    primary: BaseException,
) -> None:
    try:
        await asyncio.to_thread(
            prepared_workspace.mark_cancelled,
            "Provider invocation was cancelled during workspace success finalization.",
            child_environment_applied,
        )
    except Exception as exc:
        note_cleanup_failure(
            primary,
            "Workspace cancellation after success finalization failure",
            exc,
        )


async def snapshot_invocation_generated_files_async(
    request: ProviderCallRequest,
    prepared_workspace: PreparedWorkspace,
    change_baseline: GeneratedFileChangeBaseline | None = None,
) -> Path | None:
    snapshot_args = (
        (request, prepared_workspace, change_baseline)
        if change_baseline is not None
        else (request, prepared_workspace)
    )
    snapshot_task = asyncio.create_task(
        asyncio.to_thread(
            snapshot_invocation_generated_files,
            *snapshot_args,
        )
    )
    try:
        return await asyncio.shield(snapshot_task)
    except asyncio.CancelledError as cancel:
        await _handle_cancelled_workspace_thread_task(
            snapshot_task,
            request.runtime_context.deferred_workspace_cleanups,
            "Workspace generated-file snapshot after cancellation",
            cancel,
        )
        raise


async def _handle_cancelled_workspace_thread_task(
    task: asyncio.Task[Any],
    cleanup_registry: DeferredAsyncCleanupRegistry,
    failure_context: str,
    cancel: asyncio.CancelledError,
) -> None:
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        cleanup_registry.register(_await_workspace_thread_task(task), False)
    except Exception as exc:
        note_cleanup_failure(cancel, failure_context, exc)


async def _await_workspace_thread_task(task: asyncio.Task[Any]) -> None:
    await asyncio.shield(task)


def snapshot_invocation_generated_files(
    request: ProviderCallRequest,
    prepared_workspace: PreparedWorkspace,
    change_baseline: GeneratedFileChangeBaseline | None = None,
) -> Path | None:
    if not request.output_file.is_file():
        if request.provider_output_policy == ProviderOutputPolicy.ALLOW_MISSING_OUTPUT:
            return None
        raise RuntimeError(
            "Generated-file snapshot requires an existing provider output file: "
            f"{request.output_file.as_posix()}"
        )
    workspace_root = validated_generated_file_workspace_root(prepared_workspace)
    candidate_files = (
        change_baseline.candidate_files() if change_baseline is not None else None
    )
    if workspace_root is None:
        try:
            workspace_root = resolved_real_directory(
                prepared_workspace.cwd, "Workspace cwd"
            )
        except RuntimeError:
            return None
    return snapshot_generated_file_workspace(
        request.output_file,
        workspace_root,
        candidate_files=candidate_files,
    )


def validated_generated_file_workspace_root(
    prepared_workspace: PreparedWorkspace,
) -> Path | None:
    workspace_path = prepared_workspace.workspace_path
    if workspace_path is None:
        return None
    workspace_root = resolved_real_directory(workspace_path, "Workspace root")
    cwd = resolved_real_directory(prepared_workspace.cwd, "Workspace cwd")
    if not cwd.is_relative_to(workspace_root):
        raise RuntimeError(
            "Workspace cwd is outside the managed workspace: "
            f"{prepared_workspace.cwd.as_posix()}"
        )
    return cwd
