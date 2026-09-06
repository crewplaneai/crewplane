from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from crewplane.runtime.workspace import PreparedWorkspace

from .generated_file_changes import (
    GeneratedFileChangeBaseline,
    resolved_real_directory,
)
from .types import ProviderCallRequest, ProviderOutputPolicy


@dataclass(frozen=True)
class GeneratedFileSnapshotSource:
    provider_output_file: Path
    workspace_root: Path
    candidate_files: tuple[Path, ...] | None


def resolve_generated_file_snapshot_source(
    request: ProviderCallRequest,
    prepared_workspace: PreparedWorkspace,
    change_baseline: GeneratedFileChangeBaseline | None,
    cancel_requested: Callable[[], bool] | None,
) -> GeneratedFileSnapshotSource | None:
    provider_output_file = _resolve_snapshot_output_file(request)
    if provider_output_file is None:
        return None
    workspace_root = validated_generated_file_workspace_root(prepared_workspace)
    candidate_files = _resolve_candidate_files(change_baseline, cancel_requested)
    if workspace_root is None:
        workspace_root = _resolve_project_root_workspace(prepared_workspace)
        if workspace_root is None:
            return None
    return GeneratedFileSnapshotSource(
        provider_output_file=provider_output_file,
        workspace_root=workspace_root,
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


def _resolve_snapshot_output_file(request: ProviderCallRequest) -> Path | None:
    output_file = (
        request.invocation_output_file
        if request.defer_output_publication
        and request.invocation_output_file is not None
        else request.output_file
    )
    if output_file.is_file():
        return output_file
    if request.provider_output_policy == ProviderOutputPolicy.ALLOW_MISSING_OUTPUT:
        return None
    raise RuntimeError(
        "Generated-file snapshot requires an existing provider output file: "
        f"{output_file.as_posix()}"
    )


def _resolve_candidate_files(
    change_baseline: GeneratedFileChangeBaseline | None,
    cancel_requested: Callable[[], bool] | None,
) -> tuple[Path, ...] | None:
    if change_baseline is None:
        return None
    return change_baseline.candidate_files(cancel_requested)


def _resolve_project_root_workspace(
    prepared_workspace: PreparedWorkspace,
) -> Path | None:
    try:
        return resolved_real_directory(prepared_workspace.cwd, "Workspace cwd")
    except RuntimeError:
        return None
