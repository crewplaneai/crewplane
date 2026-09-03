from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from crewplane.core.state_paths import STATE_DIR_NAME
from crewplane.runtime.workspace.cleanup import (
    WorkspaceCleanupFilter,
    WorkspaceCleanupResult,
    cleanup_workspace_cache,
    status_matches,
)
from crewplane.runtime.workspace.worktree.ref_cleanup import (
    WorkspaceRunRefCleanup,
    workspace_ref_cleanup_for_project,
)

from ..run.workspace.git_source import GitSourceContext
from ..workspace_cleanup_evidence import WorkspaceCleanupEvidence
from .context import WorkspaceCleanupContext, workspace_cleanup_filter
from .eligibility import (
    eligible_absent_state_projections,
    inactive_ref_cleanup_runs,
    workspace_cleanup_eligibility_lookup,
)
from .run_artifacts import refresh_cleanup_workspace_descriptors

type _AbsentStateProjection = tuple[str, Path, str, tuple[Path, ...]]


@dataclass(frozen=True)
class _WorkspaceCleanupInputs:
    evidence: WorkspaceCleanupEvidence | None
    cleanup_filter: WorkspaceCleanupFilter
    absent_state_projections: tuple[_AbsentStateProjection, ...]
    inactive_ref_cleanup_run_keys: tuple[str, ...]
    ref_cleanup_run_keys: tuple[str, ...]


def execute_workspace_cleanup(
    context: WorkspaceCleanupContext,
    destructive: bool,
) -> WorkspaceCleanupResult:
    inputs = _prepare_workspace_cleanup_inputs(context)
    try:
        result = _run_workspace_cleanup(context, inputs, destructive)
    except Exception as cleanup_error:
        if destructive:
            _refresh_after_partial_cleanup(context, inputs, cleanup_error)
        raise
    if destructive:
        _reconcile_successful_workspace_cleanup(context, inputs, result)
    return result


def _prepare_workspace_cleanup_inputs(
    context: WorkspaceCleanupContext,
) -> _WorkspaceCleanupInputs:
    evidence = _workspace_cleanup_evidence(context)
    cleanup_filter = workspace_cleanup_filter(context)
    absent_state_projections = eligible_absent_state_projections(context, evidence)
    inactive_ref_cleanup_run_keys = inactive_ref_cleanup_runs(context, evidence)
    ref_cleanup_run_keys = (
        ()
        if context.statuses or context.orphans or context.older_than_seconds is not None
        else inactive_ref_cleanup_run_keys
    )
    return _WorkspaceCleanupInputs(
        evidence=evidence,
        cleanup_filter=cleanup_filter,
        absent_state_projections=absent_state_projections,
        inactive_ref_cleanup_run_keys=inactive_ref_cleanup_run_keys,
        ref_cleanup_run_keys=ref_cleanup_run_keys,
    )


def _run_workspace_cleanup(
    context: WorkspaceCleanupContext,
    inputs: _WorkspaceCleanupInputs,
    destructive: bool,
) -> WorkspaceCleanupResult:
    status_lookup = (
        unknown_workspace_status_lookup
        if inputs.evidence is None
        else inputs.evidence.status_for_cache_key
    )
    return cleanup_workspace_cache(
        context.cache_root,
        inputs.cleanup_filter,
        dry_run=not destructive,
        status_lookup=status_lookup,
        ref_cleanup=_workspace_ref_cleanup(context),
        eligibility_lookup=workspace_cleanup_eligibility_lookup(
            context,
            inputs.evidence,
        ),
        ref_cleanup_run_keys=inputs.ref_cleanup_run_keys,
        absent_state_projections=inputs.absent_state_projections,
    )


def _refresh_after_partial_cleanup(
    context: WorkspaceCleanupContext,
    inputs: _WorkspaceCleanupInputs,
    cleanup_error: Exception,
) -> None:
    try:
        refresh_cleanup_workspace_descriptors(
            context.project_root,
            inputs.inactive_ref_cleanup_run_keys,
        )
    except Exception as refresh_error:
        cleanup_error.add_note(
            "Workspace descriptor refresh after partial cleanup failed: "
            f"{refresh_error}"
        )


def _reconcile_successful_workspace_cleanup(
    context: WorkspaceCleanupContext,
    inputs: _WorkspaceCleanupInputs,
    result: WorkspaceCleanupResult,
) -> None:
    refresh_cleanup_workspace_descriptors(
        context.project_root,
        _successful_cleanup_refresh_run_keys(inputs, result),
    )


def _successful_cleanup_refresh_run_keys(
    inputs: _WorkspaceCleanupInputs,
    result: WorkspaceCleanupResult,
) -> tuple[str, ...]:
    run_key_names = set(inputs.ref_cleanup_run_keys)
    run_key_names.update(
        entry.run_key_name for entry in result.entries if entry.removed
    )
    run_key_names.update(
        run_key_name
        for run_key_name, _path, status, _state_paths in inputs.absent_state_projections
        if _absent_projection_matches_filter(
            run_key_name,
            status,
            inputs.cleanup_filter,
        )
    )
    return tuple(sorted(run_key_names))


def _absent_projection_matches_filter(
    run_key_name: str,
    status: str,
    cleanup_filter: WorkspaceCleanupFilter,
) -> bool:
    return (
        (
            cleanup_filter.run_key_name is None
            or cleanup_filter.run_key_name == run_key_name
        )
        and cleanup_filter.older_than_seconds is None
        and status_matches(status, cleanup_filter)
    )


def _workspace_cleanup_evidence(
    context: WorkspaceCleanupContext,
) -> WorkspaceCleanupEvidence | None:
    if context.all_projects:
        return None
    git_context = cast(GitSourceContext, context.scope.git_context)
    repository_id = cast(str, context.scope.repository_id)
    return WorkspaceCleanupEvidence(
        context.project_root / STATE_DIR_NAME / "execution-stages",
        context.cache_root,
        repository_id,
        git_context.common_git_dir,
    )


def _workspace_ref_cleanup(
    context: WorkspaceCleanupContext,
) -> WorkspaceRunRefCleanup | None:
    if context.all_projects:
        return None
    return workspace_ref_cleanup_for_project(context.project_root)


def unknown_workspace_status_lookup(
    run_key_name: str,  # noqa: ARG001 - Required by the cleanup callback.
    cache_key: str,  # noqa: ARG001 - Required by the cleanup callback.
) -> str:
    """Return the fallback status when project-specific evidence is unavailable.

    The cleanup callback requires run and cache identifiers even though
    cross-project cleanup cannot use them to resolve a verified status.
    """
    return "unknown"
