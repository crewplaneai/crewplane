from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from threading import Event

from crewplane.artifacts.generated_files.catalog import (
    generated_file_source_root,
    snapshot_generated_file_workspace,
)
from crewplane.runtime.workspace import PreparedWorkspace
from crewplane.runtime.workspace.cleanup_notes import note_cleanup_failure
from crewplane.runtime.workspace.state import RenderedWorkspaceFileDescriptor

from ..deferred_cleanup import DeferredAsyncCleanupRegistry, workspace_worker_task
from ..workspace_files import rendered_workspace_file_descriptor
from .cancellation import WorkspaceFinalizationDeferredCancellation
from .generated_file_changes import (
    GeneratedFileChangeBaseline,
    changed_generated_file_paths,
    resolved_real_directory,
)
from .generated_file_snapshot_source import (
    resolve_generated_file_snapshot_source,
    validated_generated_file_workspace_root,
)
from .types import ProviderCallRequest
from .workspace_worker_cancellation import (
    WorkspaceWorkerCancellation,
    WorkspaceWorkerFence,
)

__all__ = (
    "GeneratedFileChangeBaseline",
    "capture_generated_file_change_baseline",
    "capture_generated_file_change_baseline_async",
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
    cancel_requested: Callable[[], bool] | None = None,
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
        cancel_requested=cancel_requested,
    )


async def capture_generated_file_change_baseline_async(
    prepared_workspace: PreparedWorkspace,
    cleanup_registry: DeferredAsyncCleanupRegistry,
) -> GeneratedFileChangeBaseline | None:
    cancel_requested = Event()
    worker_fence = WorkspaceWorkerFence(
        getattr(prepared_workspace, "state_path", None),
        "generated_file_baseline",
    )
    baseline_task = workspace_worker_task(
        _run_generated_file_change_baseline,
        prepared_workspace,
        cancel_requested,
        worker_fence,
    )
    try:
        return await asyncio.shield(baseline_task)
    except asyncio.CancelledError as cancel:
        cancel_requested.set()
        await _workspace_worker_cancellation(cleanup_registry).wait_for_completion(
            baseline_task,
            "Workspace generated-file baseline after cancellation",
            cancel,
            worker_fence,
        )
        raise


def _run_generated_file_change_baseline(
    prepared_workspace: PreparedWorkspace,
    cancel_requested: Event,
    worker_fence: WorkspaceWorkerFence,
) -> GeneratedFileChangeBaseline | None:
    try:
        return capture_generated_file_change_baseline(
            prepared_workspace,
            cancel_requested.is_set,
        )
    finally:
        worker_fence.finish()


async def mark_workspace_succeeded(
    prepared_workspace: PreparedWorkspace,
    child_environment_applied: bool | None,
    cleanup_registry: DeferredAsyncCleanupRegistry,
) -> None:
    cancel_requested = Event()
    worker_fence = WorkspaceWorkerFence(
        getattr(prepared_workspace, "state_path", None),
        "success_finalizer",
    )
    finalization = _success_finalization_task(
        prepared_workspace,
        child_environment_applied,
        cancel_requested,
        worker_fence,
    )
    try:
        await asyncio.shield(finalization)
    except asyncio.CancelledError as cancel:
        cancel_requested.set()
        await _workspace_worker_cancellation(cleanup_registry).wait_for_completion(
            finalization,
            "Workspace success finalization after cancellation",
            cancel,
            worker_fence,
        )
        raise


async def finalize_successful_workspace(
    request: ProviderCallRequest,
    prepared_workspace: PreparedWorkspace,
    child_environment_applied: bool | None,
    generated_file_workspace: Path | None,
) -> None:
    cancel_requested = Event()
    worker_fence = WorkspaceWorkerFence(
        getattr(prepared_workspace, "state_path", None),
        "success_finalizer",
    )
    finalization = _success_finalization_task(
        prepared_workspace,
        child_environment_applied,
        cancel_requested,
        worker_fence,
    )
    try:
        await asyncio.shield(finalization)
    except asyncio.CancelledError as cancel:
        cancel_requested.set()
        finalization_deferred = await _handle_cancelled_success_finalization(
            request,
            prepared_workspace,
            child_environment_applied,
            generated_file_workspace,
            finalization,
            worker_fence,
            cancel,
        )
        if finalization_deferred:
            raise WorkspaceFinalizationDeferredCancellation(*cancel.args) from cancel
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
    worker_fence: WorkspaceWorkerFence,
    cancel: asyncio.CancelledError,
) -> bool:
    try:
        await asyncio.wait_for(
            asyncio.shield(finalization),
            WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        request.runtime_context.deferred_workspace_cleanups.register(
            _complete_deferred_success_finalization(
                request,
                prepared_workspace,
                child_environment_applied,
                generated_file_workspace,
                finalization,
            ),
            False,
        )
        await _workspace_worker_cancellation(
            request.runtime_context.deferred_workspace_cleanups
        ).persist_fence_after_timeout(
            worker_fence,
            "Workspace success finalizer fence persistence",
            cancel,
        )
        return True
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
            request.runtime_context.deferred_workspace_cleanups,
        )
        return False
    else:
        record_generated_file_workspace(
            request,
            prepared_workspace,
            generated_file_workspace,
        )
        return False


def _success_finalization_task(
    prepared_workspace: PreparedWorkspace,
    child_environment_applied: bool | None,
    cancel_requested: Event,
    worker_fence: WorkspaceWorkerFence,
) -> asyncio.Task[None]:
    return workspace_worker_task(
        _run_success_finalizer,
        prepared_workspace,
        child_environment_applied,
        cancel_requested,
        worker_fence,
    )


def _run_success_finalizer(
    prepared_workspace: PreparedWorkspace,
    child_environment_applied: bool | None,
    cancel_requested: Event,
    worker_fence: WorkspaceWorkerFence,
) -> None:
    try:
        prepared_workspace.mark_succeeded(
            child_environment_applied,
            True,
            cancel_requested.is_set,
        )
    finally:
        worker_fence.finish()


async def _complete_deferred_success_finalization(
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
            request.runtime_context.deferred_workspace_cleanups,
        )
        raise
    record_generated_file_workspace(
        request,
        prepared_workspace,
        generated_file_workspace,
    )
    _cleanup_generated_file_workspaces_after_deferred_finalization(request)


def _cleanup_generated_file_workspaces_after_deferred_finalization(
    request: ProviderCallRequest,
) -> None:
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
    cleanup_registry: DeferredAsyncCleanupRegistry,
) -> None:
    cleanup_deadline_expired = Event()
    cleanup_task = workspace_worker_task(
        prepared_workspace.mark_cancelled,
        "Provider invocation was cancelled during workspace success finalization.",
        child_environment_applied,
        cleanup_deadline_expired.is_set,
    )
    try:
        await asyncio.wait_for(
            asyncio.shield(cleanup_task),
            WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        cleanup_deadline_expired.set()
        _workspace_worker_cancellation(cleanup_registry).defer_completion(cleanup_task)
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
    cancel_requested = Event()
    worker_fence = WorkspaceWorkerFence(
        getattr(prepared_workspace, "state_path", None),
        "generated_file_snapshot",
    )
    snapshot_task = workspace_worker_task(
        _run_generated_file_snapshot,
        request,
        prepared_workspace,
        change_baseline,
        cancel_requested,
        worker_fence,
    )
    try:
        return await asyncio.shield(snapshot_task)
    except asyncio.CancelledError as cancel:
        cancel_requested.set()
        await _workspace_worker_cancellation(
            request.runtime_context.deferred_workspace_cleanups
        ).wait_for_completion(
            snapshot_task,
            "Workspace generated-file snapshot after cancellation",
            cancel,
            worker_fence,
        )
        raise


def _run_generated_file_snapshot(
    request: ProviderCallRequest,
    prepared_workspace: PreparedWorkspace,
    change_baseline: GeneratedFileChangeBaseline | None,
    cancel_requested: Event,
    worker_fence: WorkspaceWorkerFence,
) -> Path | None:
    try:
        return snapshot_invocation_generated_files(
            request,
            prepared_workspace,
            change_baseline,
            cancel_requested.is_set,
        )
    finally:
        worker_fence.finish()


def _workspace_worker_cancellation(
    cleanup_registry: DeferredAsyncCleanupRegistry,
) -> WorkspaceWorkerCancellation:
    return WorkspaceWorkerCancellation(
        cleanup_registry,
        WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS,
    )


def snapshot_invocation_generated_files(
    request: ProviderCallRequest,
    prepared_workspace: PreparedWorkspace,
    change_baseline: GeneratedFileChangeBaseline | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> Path | None:
    source = resolve_generated_file_snapshot_source(
        request,
        prepared_workspace,
        change_baseline,
        cancel_requested,
    )
    if source is None:
        return None
    snapshot_root = generated_file_source_root(request.output_file)
    if request.on_generated_file_snapshot_started is not None:
        request.on_generated_file_snapshot_started(snapshot_root)
    succeeded = False
    published_signatures: dict[Path, tuple[int, str]] = {}
    try:
        result = snapshot_generated_file_workspace(
            source.provider_output_file,
            source.workspace_root,
            candidate_files=source.candidate_files,
            explicit_claims_only=prepared_workspace.workspace_kind == "project_root",
            on_file_published=published_signatures.__setitem__,
            snapshot_root=snapshot_root,
        )
        succeeded = True
        return result
    finally:
        if request.on_generated_file_snapshot_finished is not None:
            request.on_generated_file_snapshot_finished(
                snapshot_root,
                dict(published_signatures) if succeeded else None,
            )
