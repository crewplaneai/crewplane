from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from typing import Any

from orchestrator_cli.artifacts.generated_files.catalog import (
    snapshot_generated_file_workspace,
)
from orchestrator_cli.runtime.workspace import PreparedWorkspace
from orchestrator_cli.runtime.workspace.cleanup_notes import note_cleanup_failure
from orchestrator_cli.runtime.workspace.snapshot import snapshot_entries
from orchestrator_cli.runtime.workspace.state import RenderedWorkspaceFileDescriptor

from ..runtime_context import DeferredAsyncCleanupRegistry
from ..workspace_files import rendered_workspace_file_descriptor
from .types import ProviderCallRequest

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
    if prepared_workspace.workspace_path is None:
        return
    cleanup = (
        prepared_workspace.cleanup_after_success
        if prepared_workspace.cleanup_on_success
        else None
    )
    request.runtime_context.generated_file_workspaces.record(
        request.node_id,
        request.output_file,
        workspace_root,
        cleanup,
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
        cancel._orchestrator_workspace_finalization_deferred = True  # type: ignore[attr-defined]
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
) -> Path | None:
    snapshot_task = asyncio.create_task(
        asyncio.to_thread(
            snapshot_invocation_generated_files,
            request,
            prepared_workspace,
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
) -> Path | None:
    workspace_root = validated_generated_file_workspace_root(prepared_workspace)
    if workspace_root is None:
        return None
    return snapshot_generated_file_workspace(
        request.output_file,
        workspace_root,
        changed_generated_file_paths(prepared_workspace, workspace_root),
    )


def changed_generated_file_paths(
    prepared_workspace: PreparedWorkspace,
    workspace_root: Path,
) -> set[str]:
    workspace = prepared_workspace.invocation_context.workspace
    if workspace is None or workspace.checkout_root is None:
        return set()
    checkout_root = workspace.checkout_root
    changed_paths = changed_checkout_paths(prepared_workspace, checkout_root)
    return _paths_relative_to_workspace_root(
        changed_paths,
        checkout_root,
        workspace_root,
    )


def changed_checkout_paths(
    prepared_workspace: PreparedWorkspace,
    checkout_root: Path,
) -> tuple[str, ...]:
    if prepared_workspace.workspace_kind not in {"snapshot", "worktree"}:
        return ()
    current_entries = snapshot_entries(checkout_root)
    initial_entries = prepared_workspace.initial_snapshot_entries or {}
    return tuple(
        sorted(
            path
            for path in set(initial_entries) | set(current_entries)
            if initial_entries.get(path) != current_entries.get(path)
        )
    )


def validated_generated_file_workspace_root(
    prepared_workspace: PreparedWorkspace,
) -> Path | None:
    workspace_path = prepared_workspace.workspace_path
    if workspace_path is None:
        return None
    workspace_root = _resolved_real_directory(workspace_path, "Workspace root")
    cwd = _resolved_real_directory(prepared_workspace.cwd, "Workspace cwd")
    if not cwd.is_relative_to(workspace_root):
        raise RuntimeError(
            "Workspace cwd is outside the managed workspace: "
            f"{prepared_workspace.cwd.as_posix()}"
        )
    return cwd


def _paths_relative_to_workspace_root(
    changed_paths: tuple[str, ...],
    checkout_root: Path,
    workspace_root: Path,
) -> set[str]:
    relative_paths: set[str] = set()
    resolved_workspace_root = workspace_root.resolve(strict=True)
    for changed_path in changed_paths:
        candidate = checkout_root.joinpath(*Path(changed_path).parts)
        try:
            relative_path = candidate.resolve(strict=False).relative_to(
                resolved_workspace_root
            )
        except (OSError, ValueError):
            continue
        if not relative_path.parts:
            continue
        relative_paths.add(relative_path.as_posix())
    return relative_paths


def _resolved_real_directory(path: Path, label: str) -> Path:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} is missing: {path.as_posix()}") from exc
    if not stat.S_ISDIR(mode):
        raise RuntimeError(f"{label} is not a real directory: {path.as_posix()}")
    return path.resolve(strict=True)
