from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.value_checks import is_strict_int
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workspace.git_policy import git_ref_syntax_issue, is_git_object_id
from crewplane.core.workspace.policy import WorkspaceMaterialization

from ..chain_validation import verify_persisted_workspace_result_chain
from .fields import WorkspaceArtifactRoot, mapping_value


def workspace_materialization_result_matches(
    materialization: WorkspaceMaterialization,
    source: WorkspaceArtifactRoot,
    plan: PreflightExecutionPlan,
    payload: dict[str, object],
) -> bool:
    """Validate disposable results or a persisted lineage result and bundle."""
    workspace = mapping_value(payload.get("workspace"))
    if workspace.get("writable") is not True:
        return False
    match materialization:
        case "snapshot_checkout":
            return _snapshot_result_matches(payload)
        case "worktree_checkout":
            if workspace.get("lineage_producer") is not True:
                return _disposable_worktree_result_matches(payload)
            return (
                payload.get("role") == ProviderRole.EXECUTOR
                and _workspace_result_matches(payload)
                and _workspace_bundle_matches(source, plan, payload)
            )
        case _:
            return False


def _workspace_result_matches(payload: dict[str, object]) -> bool:
    result = mapping_value(payload.get("result"))
    return (
        is_git_object_id(result.get("candidate_commit"))
        and is_git_object_id(result.get("result_commit"))
        and is_git_object_id(result.get("candidate_tree"))
        and is_git_object_id(result.get("result_tree"))
        and is_strict_int(result.get("changed_path_count"))
        and result.get("unreachable_provider_objects_scanned") is False
    )


def _snapshot_result_matches(payload: dict[str, object]) -> bool:
    result = mapping_value(payload.get("result"))
    if result.get("drift_scan_complete") is False:
        return (
            result.get("lineage_produced") is False
            and isinstance(result.get("drift_scan_limit_reason"), str)
            and "bundle" not in payload
        )
    if result.get("drift_scan_complete") is not True:
        return False
    changed_path_count = result.get("changed_path_count")
    changed_paths = result.get("changed_paths")
    if not is_strict_int(changed_path_count):
        return False
    snapshot_drift_discarded = changed_path_count > 0
    return (
        result.get("lineage_produced") is False
        and result.get("snapshot_drift_discarded") is snapshot_drift_discarded
        and isinstance(changed_paths, list)
        and all(isinstance(path, str) for path in changed_paths)
        and isinstance(result.get("changed_paths_truncated"), bool)
        and _non_lineage_result_excludes_lineage_evidence(result, payload)
    )


def _disposable_worktree_result_matches(payload: dict[str, object]) -> bool:
    result = mapping_value(payload.get("result"))
    changed_path_count = result.get("changed_path_count")
    return (
        is_strict_int(changed_path_count)
        and result.get("lineage_produced") is False
        and is_git_object_id(result.get("final_head"))
        and _non_lineage_result_excludes_lineage_evidence(result, payload)
    )


def _non_lineage_result_excludes_lineage_evidence(
    result: Mapping[str, object],
    payload: dict[str, object],
) -> bool:
    return (
        "candidate_commit" not in result
        and "result_commit" not in result
        and "candidate_tree" not in result
        and "result_tree" not in result
        and "bundle" not in payload
    )


def _workspace_bundle_matches(
    source: WorkspaceArtifactRoot,
    plan: PreflightExecutionPlan,
    payload: dict[str, object],
) -> bool:
    result_ref = _workspace_result_ref(payload)
    result_commit = _workspace_result_commit(payload)
    result_tree = _workspace_result_tree(payload)
    workspace_source = plan.workspace_source
    if (
        result_ref is None
        or result_commit is None
        or result_tree is None
        or workspace_source is None
    ):
        return False
    if not _bundle_file_matches(source.run_dir, mapping_value(payload.get("bundle"))):
        return False
    try:
        verify_persisted_workspace_result_chain(
            workspace_source,
            source.run_dir,
            payload,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False
    return True


def _bundle_file_matches(run_dir: Path, bundle: Mapping[str, object]) -> bool:
    path = bundle.get("path")
    sha256 = bundle.get("sha256")
    size_bytes = bundle.get("size_bytes")
    if (
        not isinstance(path, str)
        or not isinstance(sha256, str)
        or not is_strict_int(size_bytes)
        or bundle.get("verified") is not True
    ):
        return False
    bundle_path = contained_regular_file(run_dir, path)
    if bundle_path is None:
        return False
    try:
        actual_size, actual_sha256 = file_size_and_sha256(bundle_path)
    except OSError:
        return False
    return actual_size == size_bytes and actual_sha256 == sha256


def _workspace_result_ref(payload: dict[str, object]) -> str | None:
    refs = mapping_value(payload.get("refs"))
    result_ref = refs.get("result")
    if not isinstance(result_ref, str) or not _safe_workspace_result_ref(result_ref):
        return None
    return result_ref


def _workspace_result_commit(payload: dict[str, object]) -> str | None:
    result = mapping_value(payload.get("result"))
    result_commit = result.get("result_commit")
    return result_commit if is_git_object_id(result_commit) else None


def _workspace_result_tree(payload: dict[str, object]) -> str | None:
    result = mapping_value(payload.get("result"))
    result_tree = result.get("result_tree")
    return result_tree if is_git_object_id(result_tree) else None


def _safe_workspace_result_ref(ref: str) -> bool:
    return (
        ref.startswith("refs/crewplane/")
        and ref.endswith("/result")
        and git_ref_syntax_issue(ref) is None
    )
