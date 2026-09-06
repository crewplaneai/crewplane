from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crewplane.artifacts.locks import run_lock_activity
from crewplane.artifacts.locks.manifest import LockManifestError, LockRunMetadata
from crewplane.artifacts.locks.process_identity import ProcessInspector
from crewplane.artifacts.locks.provider_processes import (
    ensure_no_live_provider_processes,
)
from crewplane.core.execution_state import RunManifest
from crewplane.core.state_paths import STATE_DIR_NAME
from crewplane.runtime.workspace.cleanup import (
    AbsentWorkspaceStateProjection,
    WorkspaceCleanupEligibility,
    WorkspaceCleanupEligibilityLookup,
)

from ..workspace_cleanup_evidence import WorkspaceCleanupEvidence
from .context import WorkspaceCleanupContext
from .run_artifacts import load_workspace_manifest


@dataclass(frozen=True)
class _EligibilityChecks:
    state_dir: Path
    orphan_cleanup_requested: bool
    inspector: ProcessInspector


def inactive_ref_cleanup_runs(
    context: WorkspaceCleanupContext,
    evidence: WorkspaceCleanupEvidence | None,
) -> tuple[str, ...]:
    if evidence is None:
        return ()
    checks = _eligibility_checks(context)
    inactive: list[str] = []
    for run_key_name in evidence.ref_cleanup_run_keys():
        if context.run_key_name is not None and run_key_name != context.run_key_name:
            continue
        manifest, _reason = _eligible_workspace_manifest(
            checks.state_dir,
            run_key_name,
        )
        if manifest is None or _run_has_activity(run_key_name, manifest, checks):
            continue
        inactive.append(run_key_name)
    return tuple(inactive)


def eligible_absent_state_projections(
    context: WorkspaceCleanupContext,
    evidence: WorkspaceCleanupEvidence | None,
) -> tuple[AbsentWorkspaceStateProjection, ...]:
    if evidence is None:
        return ()
    checks = _eligibility_checks(context)
    return tuple(
        projection
        for projection in evidence.absent_state_projections()
        if _first_cleanup_blocker_reason(
            projection.run_key_name,
            projection.status,
            checks,
        )
        is None
    )


def workspace_cleanup_eligibility_lookup(
    context: WorkspaceCleanupContext,
    evidence: WorkspaceCleanupEvidence | None = None,
) -> WorkspaceCleanupEligibilityLookup | None:
    """Return project-scoped cleanup checks or no cross-project override."""
    if context.all_projects:
        return None
    checks = _eligibility_checks(context)

    def current_project_lookup(
        run_key_name: str,
        workspace_path: Path,
        status: str | None,
    ) -> WorkspaceCleanupEligibility:
        evidence_decision = (
            evidence.decision(run_key_name, workspace_path)
            if evidence is not None
            else None
        )
        if evidence_decision is not None and not evidence_decision.deletable:
            return WorkspaceCleanupEligibility(
                deletable=False,
                reason=evidence_decision.reason,
                state_paths=evidence_decision.state_paths,
                expected_worktree_git_dir=(evidence_decision.expected_worktree_git_dir),
            )
        blocker_reason = _first_cleanup_blocker_reason(
            run_key_name,
            status,
            checks,
        )
        return WorkspaceCleanupEligibility(
            deletable=blocker_reason is None,
            reason=blocker_reason,
            state_paths=(
                evidence_decision.state_paths if evidence_decision is not None else ()
            ),
            expected_worktree_git_dir=(
                evidence_decision.expected_worktree_git_dir
                if evidence_decision is not None
                else None
            ),
        )

    return current_project_lookup


def _eligibility_checks(context: WorkspaceCleanupContext) -> _EligibilityChecks:
    return _EligibilityChecks(
        state_dir=context.project_root / STATE_DIR_NAME,
        orphan_cleanup_requested=context.orphans,
        inspector=ProcessInspector(),
    )


def _first_cleanup_blocker_reason(
    run_key_name: str,
    status: str | None,
    checks: _EligibilityChecks,
) -> str | None:
    workspace_state_reason = _workspace_state_blocker_reason(
        status,
        checks.orphan_cleanup_requested,
    )
    if workspace_state_reason is not None:
        return workspace_state_reason

    manifest, manifest_reason = _eligible_workspace_manifest(
        checks.state_dir,
        run_key_name,
    )
    if manifest is None:
        return manifest_reason
    return _activity_blocker_reason(run_key_name, manifest, checks)


def _workspace_state_blocker_reason(
    workspace_state: str | None,
    orphan_cleanup_requested: bool,
) -> str | None:
    if workspace_state is None:
        if orphan_cleanup_requested:
            return None
        return "workspace state is unverifiable"
    if workspace_state not in {"succeeded", "failed", "cancelled"}:
        return f"workspace state is {workspace_state}"
    return None


def _eligible_workspace_manifest(
    state_dir: Path,
    run_key_name: str,
) -> tuple[RunManifest | None, str | None]:
    manifest_load = load_workspace_manifest(state_dir, run_key_name)
    manifest = manifest_load.manifest
    if manifest is None:
        return None, manifest_load.error
    if manifest.run_key_name != run_key_name or manifest.status == "running":
        return None, "run manifest is active or mismatched"
    return manifest, None


def _run_has_activity(
    run_key_name: str,
    manifest: RunManifest,
    checks: _EligibilityChecks,
) -> bool:
    return _activity_blocker_reason(run_key_name, manifest, checks) is not None


def _activity_blocker_reason(
    run_key_name: str,
    manifest: RunManifest,
    checks: _EligibilityChecks,
) -> str | None:
    run_lock_reason = _run_lock_blocker(
        checks.state_dir,
        run_key_name,
        checks.inspector,
    )
    if run_lock_reason is not None:
        return run_lock_reason
    return _provider_process_blocker(
        checks.state_dir,
        manifest,
        checks.inspector,
    )


def _run_lock_blocker(
    state_dir: Path,
    run_key_name: str,
    inspector: ProcessInspector,
) -> str | None:
    try:
        lock_activity = run_lock_activity(state_dir, run_key_name, inspector)
    except (OSError, RuntimeError):
        lock_activity = "unverifiable"
    if lock_activity in {"live", "unverifiable"}:
        return f"run lock is {lock_activity}"
    return None


def _provider_process_blocker(
    state_dir: Path,
    manifest: RunManifest,
    inspector: ProcessInspector,
) -> str | None:
    metadata = LockRunMetadata(
        run_id=manifest.run_id,
        run_key_name=manifest.run_key_name,
        workflow_identity=manifest.workflow_identity,
        workflow_signature=manifest.workflow_signature,
    )
    try:
        ensure_no_live_provider_processes(state_dir, metadata, inspector)
    except (LockManifestError, OSError, RuntimeError) as exc:
        return str(exc) or "provider process state is unverifiable"
    return None
