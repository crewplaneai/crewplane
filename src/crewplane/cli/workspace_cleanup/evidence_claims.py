from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from crewplane.artifacts.workspace.state.contracts import (
    workspace_state_contract_errors,
)
from crewplane.artifacts.workspace.state.ref_contracts import is_discarded_lineage
from crewplane.core.execution_state import RunManifest
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
)

from .evidence_discovery import (
    path_has_symlink_component,
    single_link_regular_file,
)


@dataclass(frozen=True)
class WorkspaceClaim:
    state_path: Path
    payload: dict[str, object]
    repository_id: str
    run_key_name: str
    workspace_path: Path
    workspace_kind: str
    common_git_dir: Path
    worktree_git_dir: Path | None
    generation: int | None


@dataclass(frozen=True)
class AuthoritativePlanLoad:
    plan: PreflightExecutionPlan | None
    invalid: bool


@dataclass(frozen=True)
class WorkspaceClaimLoad:
    claim: WorkspaceClaim | None
    invalid: bool


@dataclass(frozen=True)
class _AuthoritativeDescriptors:
    plan: PreflightExecutionPlan
    manifest: RunManifest


@dataclass(frozen=True)
class _ClaimSections:
    run_key_name: str
    git: dict[str, object]
    workspace: dict[str, object]
    execution: dict[str, object]
    workspace_path: Path


def load_authoritative_plan(
    run_dir: Path,
    run_key_name: str,
) -> AuthoritativePlanLoad:
    descriptor_text = _read_authoritative_descriptors(run_dir)
    if descriptor_text is None:
        return AuthoritativePlanLoad(None, True)
    descriptors = _decode_authoritative_descriptors(*descriptor_text)
    if descriptors is None:
        return AuthoritativePlanLoad(None, True)
    if not _plan_manifest_identity_matches(
        descriptors.plan,
        descriptors.manifest,
        run_key_name,
        run_dir,
    ):
        return AuthoritativePlanLoad(None, True)
    return AuthoritativePlanLoad(descriptors.plan, False)


def load_workspace_claim(
    state_path: Path,
    directory_run_key: str,
    expected_node_id: str,
    plan: PreflightExecutionPlan,
    cache_root: Path,
) -> WorkspaceClaimLoad:
    payload = _read_claim_payload(state_path)
    if payload is None:
        return WorkspaceClaimLoad(None, True)
    if not _claim_identity_matches(
        payload,
        directory_run_key,
        expected_node_id,
        plan,
    ):
        return WorkspaceClaimLoad(None, True)
    sections = _claim_sections(payload)
    if sections is None:
        return WorkspaceClaimLoad(
            None,
            not _is_hydrated_resume_without_workspace_path(payload),
        )
    policy = _workspace_policy(plan, expected_node_id)
    if not _claim_matches_workspace_policy(payload, policy):
        return WorkspaceClaimLoad(None, True)
    claim = _construct_claim(state_path, payload, sections)
    if not _claim_placement_is_coherent(sections, cache_root):
        return WorkspaceClaimLoad(None, True)
    return WorkspaceClaimLoad(claim, False)


def claim_retention(claim: WorkspaceClaim) -> object:
    workspace = claim.payload.get("workspace")
    return workspace.get("retention") if isinstance(workspace, dict) else None


def claim_has_pending_ref_cleanup(claim: WorkspaceClaim) -> bool:
    publication = claim.payload.get("ref_publication")
    if isinstance(publication, dict) and publication.get("phase") != "removed":
        return True
    temporary_refs = claim.payload.get("temporary_refs")
    return isinstance(temporary_refs, list) and any(
        not isinstance(record, dict) or record.get("phase") != "removed"
        for record in temporary_refs
    )


def _read_authoritative_descriptors(run_dir: Path) -> tuple[str, str] | None:
    plan_path = run_dir / "preflight" / "execution-plan.json"
    manifest_path = run_dir / "manifests" / "run.json"
    if not _descriptor_is_safe(run_dir, plan_path) or not _descriptor_is_safe(
        run_dir, manifest_path
    ):
        return None
    try:
        return (
            plan_path.read_text(encoding="utf-8"),
            manifest_path.read_text(encoding="utf-8"),
        )
    except (OSError, UnicodeDecodeError):
        return None


def _descriptor_is_safe(run_dir: Path, path: Path) -> bool:
    try:
        path.lstat()
    except OSError:
        return False
    return not path_has_symlink_component(path, run_dir) and single_link_regular_file(
        path
    )


def _decode_authoritative_descriptors(
    plan_text: str,
    manifest_text: str,
) -> _AuthoritativeDescriptors | None:
    try:
        return _AuthoritativeDescriptors(
            plan=PreflightExecutionPlan.model_validate_json(plan_text),
            manifest=RunManifest.model_validate_json(manifest_text),
        )
    except ValueError:
        return None


def _read_claim_payload(state_path: Path) -> dict[str, object] | None:
    if not single_link_regular_file(state_path):
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _claim_identity_matches(
    payload: dict[str, object],
    directory_run_key: str,
    expected_node_id: str,
    plan: PreflightExecutionPlan,
) -> bool:
    run_key_name = payload.get("run_key_name")
    return (
        isinstance(run_key_name, str)
        and payload.get("run_id") == plan.run_id
        and run_key_name == plan.run_key_name == directory_run_key
        and payload.get("workflow_name") == plan.workflow_name
        and payload.get("workflow_signature") == plan.workflow_signature
        and payload.get("node_id") == expected_node_id
    )


def _claim_sections(payload: dict[str, object]) -> _ClaimSections | None:
    execution = payload.get("execution")
    workspace_path_value = (
        execution.get("workspace_path") if isinstance(execution, dict) else None
    )
    workspace_path = _normalized_absolute_path(workspace_path_value)
    if workspace_path is None:
        return None
    run_key_name = payload.get("run_key_name")
    git = payload.get("git")
    workspace = payload.get("workspace")
    if not (
        isinstance(run_key_name, str)
        and isinstance(git, dict)
        and isinstance(workspace, dict)
        and isinstance(execution, dict)
        and isinstance(git.get("repo_id"), str)
        and isinstance(git.get("common_git_dir"), str)
        and isinstance(payload.get("workspace_kind"), str)
    ):
        return None
    return _ClaimSections(run_key_name, git, workspace, execution, workspace_path)


def _is_hydrated_resume_without_workspace_path(payload: dict[str, object]) -> bool:
    execution = payload.get("execution")
    if isinstance(execution, dict) and isinstance(execution.get("workspace_path"), str):
        return False
    workspace = payload.get("workspace")
    return (
        isinstance(workspace, dict)
        and workspace.get("retention") == "not_applicable"
        and workspace.get("retained_reason") == "hydrated_resume"
        and not workspace_state_contract_errors(
            payload,
            "failed_invocation"
            if payload.get("status") == "failed"
            else "duplicate_skip",
        )
    )


def _workspace_policy(
    plan: PreflightExecutionPlan,
    expected_node_id: str,
) -> WorkspaceSelectionRecord | None:
    return next(
        (node.workspace_policy for node in plan.nodes if node.id == expected_node_id),
        None,
    )


def _claim_matches_workspace_policy(
    payload: dict[str, object],
    policy: WorkspaceSelectionRecord | None,
) -> bool:
    workspace = payload.get("workspace")
    if policy is None or not policy.enabled or not isinstance(workspace, dict):
        return False
    expected_lineage_producer = (
        policy.declaration_kind == "worktree"
        and payload.get("role") == "executor"
        and not is_discarded_lineage(payload)
    )
    return (
        payload.get("workspace_kind") == policy.declaration_kind
        and payload.get("logical_worktree_name") == policy.logical_worktree_name
        and payload.get("clean_start") == policy.clean_start
        and payload.get("worktree_contract")
        == policy.worktree_contract.model_dump(mode="json")
        and workspace.get("materialization") == policy.materialization
        and workspace.get("writable") is policy.writable
        and workspace.get("lineage_producer") is expected_lineage_producer
    )


def _construct_claim(
    state_path: Path,
    payload: dict[str, object],
    sections: _ClaimSections,
) -> WorkspaceClaim:
    generation = sections.workspace.get("reuse_generation")
    if not isinstance(generation, int) or isinstance(generation, bool):
        generation = None
    worktree_git_dir = sections.execution.get("worktree_git_dir")
    return WorkspaceClaim(
        state_path=state_path,
        payload=payload,
        repository_id=str(sections.git["repo_id"]),
        run_key_name=sections.run_key_name,
        workspace_path=sections.workspace_path,
        workspace_kind=str(payload["workspace_kind"]),
        common_git_dir=Path(str(sections.git["common_git_dir"])).resolve(strict=False),
        worktree_git_dir=(
            Path(worktree_git_dir).resolve(strict=False)
            if isinstance(worktree_git_dir, str)
            else None
        ),
        generation=generation,
    )


def _claim_placement_is_coherent(
    sections: _ClaimSections,
    cache_root: Path,
) -> bool:
    workspace_path = sections.workspace_path
    checkout_root = workspace_path / "checkout"
    optional_paths = (
        (sections.workspace.get("path"), workspace_path),
        (sections.workspace.get("cache_root"), cache_root),
        (sections.workspace.get("checkout_root"), checkout_root),
    )
    required_paths = (
        (sections.execution.get("cache_root"), cache_root),
        (sections.execution.get("workspace_path"), workspace_path),
        (sections.execution.get("checkout_root"), checkout_root),
    )
    if sections.workspace.get("cache_key") != workspace_path.name:
        return False
    if not all(
        _optional_path_matches(value, expected) for value, expected in optional_paths
    ):
        return False
    if not all(
        _required_path_matches(value, expected) for value, expected in required_paths
    ):
        return False
    expected_cwd = _expected_effective_cwd(sections.workspace, checkout_root)
    return expected_cwd is not None and all(
        _optional_path_matches(section.get("effective_cwd"), expected_cwd)
        for section in (sections.workspace, sections.execution)
    )


def _expected_effective_cwd(
    workspace: dict[str, object],
    checkout_root: Path,
) -> Path | None:
    project_relative = workspace.get("project_root_relative_path")
    if not isinstance(project_relative, str):
        return None
    relative_path = Path(project_relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None
    return (checkout_root / relative_path).resolve(strict=False)


def _plan_manifest_identity_matches(
    plan: PreflightExecutionPlan,
    manifest: RunManifest,
    run_key_name: str,
    run_dir: Path,
) -> bool:
    return (
        manifest.preflight_plan_path == "preflight/execution-plan.json"
        and Path(plan.context_root).resolve(strict=False)
        == run_dir.resolve(strict=False)
        and Path(plan.manifest_root).resolve(strict=False)
        == (run_dir / "manifests").resolve(strict=False)
        and plan.run_id == manifest.run_id
        and plan.run_key_name == manifest.run_key_name == run_key_name
        and plan.plan_schema_version == manifest.plan_schema_version
        and plan.workflow_name == manifest.workflow_name
        and plan.workflow_signature == manifest.workflow_signature
        and plan.effective_runtime_config_signature
        == manifest.effective_runtime_config_signature
    )


def _normalized_absolute_path(value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _required_path_matches(value: object, expected: Path) -> bool:
    return _normalized_absolute_path(value) == expected.resolve(strict=False)


def _optional_path_matches(value: object, expected: Path) -> bool:
    return value is None or _required_path_matches(value, expected)
