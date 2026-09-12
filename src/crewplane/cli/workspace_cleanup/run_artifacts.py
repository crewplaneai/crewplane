from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.architecture.safe_files import (
    contained_directory,
    contained_regular_file,
)
from crewplane.artifacts.naming import run_manifest_relative_path
from crewplane.artifacts.workspace.node_state import refresh_node_workspace_descriptor
from crewplane.core.execution_state import RunManifest
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.state_paths import STATE_DIR_NAME


@dataclass(frozen=True)
class WorkspaceManifestLoad:
    manifest: RunManifest | None
    error: str | None


@dataclass(frozen=True)
class _CleanupNodeArtifactStateStore:
    stages_dir: Path

    def get_node_dir(self, request: NodeArtifactRequest) -> Path | None:
        stage_path = request.contract.stage_path
        if stage_path is None:
            return None
        return contained_directory(self.stages_dir, stage_path)


def refresh_cleanup_workspace_descriptors(
    project_root: Path,
    run_key_names: tuple[str, ...],
) -> None:
    state_dir = project_root / STATE_DIR_NAME
    for run_key_name in run_key_names:
        manifest_path = _run_manifest_path(state_dir, run_key_name)
        if manifest_path is None:
            continue
        run_dir = manifest_path.parent.parent
        node_manifest_dir = contained_directory(run_dir, "manifests/nodes")
        if node_manifest_dir is None or not any(node_manifest_dir.iterdir()):
            continue
        plan = _load_cleanup_preflight_plan(run_dir, manifest_path, run_key_name)
        store = _CleanupNodeArtifactStateStore(run_dir)
        for node in plan.nodes:
            policy = node.workspace_policy
            if policy is not None and policy.enabled:
                refresh_node_workspace_descriptor(node, plan, store)


def load_workspace_manifest(
    state_dir: Path,
    run_key_name: str,
) -> WorkspaceManifestLoad:
    manifest_path = _run_manifest_path(state_dir, run_key_name)
    if manifest_path is None:
        return WorkspaceManifestLoad(None, "run manifest is missing or unsafe")
    return _read_workspace_manifest(manifest_path)


def _load_cleanup_preflight_plan(
    run_dir: Path,
    manifest_path: Path,
    run_key_name: str,
) -> PreflightExecutionPlan:
    manifest_load = _read_workspace_manifest(manifest_path)
    manifest = manifest_load.manifest
    if manifest is None or manifest.run_key_name != run_key_name:
        raise RuntimeError(
            f"Cannot refresh workspace descriptors for '{run_key_name}'."
        )
    plan = _load_cleanup_preflight_plan_file(run_dir, manifest, run_key_name)
    _validate_cleanup_preflight_plan_identity(plan, manifest, run_key_name)
    return plan


def _load_cleanup_preflight_plan_file(
    run_dir: Path,
    manifest: RunManifest,
    run_key_name: str,
) -> PreflightExecutionPlan:
    plan_path = contained_regular_file(run_dir, manifest.preflight_plan_path)
    if plan_path is None:
        raise RuntimeError(f"Preflight plan is missing for run '{run_key_name}'.")
    try:
        return PreflightExecutionPlan.model_validate_json(
            plan_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Preflight plan is invalid for run '{run_key_name}'."
        ) from exc


def _validate_cleanup_preflight_plan_identity(
    plan: PreflightExecutionPlan,
    manifest: RunManifest,
    run_key_name: str,
) -> None:
    if (
        plan.run_id != manifest.run_id
        or plan.run_key_name != manifest.run_key_name
        or plan.workflow_name != manifest.workflow_name
        or plan.workflow_signature != manifest.workflow_signature
    ):
        raise RuntimeError(
            f"Preflight plan identity is invalid for run '{run_key_name}'."
        )


def _run_manifest_path(state_dir: Path, run_key_name: str) -> Path | None:
    try:
        return contained_regular_file(
            state_dir / "execution-stages",
            f"{run_key_name}/{run_manifest_relative_path().as_posix()}",
        )
    except OSError:
        return None


def _read_workspace_manifest(manifest_path: Path) -> WorkspaceManifestLoad:
    try:
        manifest = RunManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return WorkspaceManifestLoad(None, "run manifest is invalid")
    return WorkspaceManifestLoad(manifest, None)
