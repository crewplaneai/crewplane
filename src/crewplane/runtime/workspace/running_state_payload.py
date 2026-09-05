from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from crewplane.version import SCHEMA_VERSION

if TYPE_CHECKING:
    from crewplane.core.preflight.models import (
        PreflightExecutionNode,
        WorkspaceSelectionRecord,
        WorkspaceSourceSnapshot,
    )

    from .state import (
        WorkspaceStateMaterializationRequest,
        WorkspaceStateWriteRequest,
    )
    from .worktree.types import WorktreeSourceRef


def build_running_workspace_state_payload(
    state_path: Path,
    request: WorkspaceStateWriteRequest,
    node: PreflightExecutionNode,
    source: WorkspaceSourceSnapshot,
    policy: WorkspaceSelectionRecord,
    materialization: WorkspaceStateMaterializationRequest,
    updated_at: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": SCHEMA_VERSION,
        "run_id": request.run_id,
        "run_key_name": request.run_key_name,
        "workflow_name": request.workflow_name,
        "workflow_signature": request.workflow_signature,
        "node_id": node.id,
        "task_id": request.task_id,
        "provider": request.provider,
        "role": request.role_label,
        "round_num": request.round_num,
        "audit_round_num": request.audit_round_num,
        "status": "running",
        "logical_worktree_name": policy.logical_worktree_name,
        "workspace_kind": policy.declaration_kind,
        "clean_start": policy.clean_start,
        "worktree_contract": policy.worktree_contract.model_dump(mode="json"),
        "git": _git_payload(source, materialization),
        "source": _source_payload(materialization.source_ref, source, state_path),
        "workspace": _workspace_payload(source, materialization),
        "execution": _execution_payload(materialization),
        "invocation_source": _invocation_source_payload(
            materialization.source_ref,
            source,
            state_path,
        ),
        "child_process_environment": {
            "required": materialization.child_environment_required,
            "applied": (False if materialization.child_environment_required else None),
        },
        "invoker": request.invoker,
        "rendered_workspace_files": list(request.rendered_workspace_files),
        "diagnostics": [],
        "process_drain": {"status": "not_started"},
        "updated_at": updated_at,
    }
    _add_reuse_payload(payload, materialization)
    _add_setup_payload(payload, policy)
    return payload


def _git_payload(
    source: WorkspaceSourceSnapshot,
    materialization: WorkspaceStateMaterializationRequest,
) -> dict[str, object]:
    return {
        "object_format": source.object_format,
        "repo_id": source.repository_id,
        "run_base_commit": source.run_base_commit,
        "source_tree": source.source_tree,
        "git_top_level": source.git_top_level,
        "active_git_dir": source.active_git_dir,
        "common_git_dir": source.common_git_dir,
        "worktree_config_active": False,
        "worktree_lock_mode": materialization.worktree_lock_mode,
    }


def _workspace_payload(
    source: WorkspaceSourceSnapshot,
    materialization: WorkspaceStateMaterializationRequest,
) -> dict[str, object]:
    return {
        "path": None,
        "effective_cwd": None,
        "cache_key": materialization.workspace_path.name,
        "materialization": materialization.materialization,
        "writable": materialization.writable,
        "lineage_producer": materialization.lineage_producer,
        "retention": "pending",
        "retained_reason": None,
        "project_root_relative_path": source.project_root_relative_path,
    }


def _execution_payload(
    materialization: WorkspaceStateMaterializationRequest,
) -> dict[str, object]:
    provisioning = materialization.provisioning
    return {
        "cache_root": materialization.cache_root,
        "workspace_path": materialization.workspace_path.as_posix(),
        "checkout_root": _optional_path(materialization.checkout_root),
        "worktree_git_dir": _optional_path(materialization.worktree_git_dir),
        "checkout_size_bytes": (
            provisioning.checkout_size_bytes if provisioning is not None else None
        ),
        "effective_cwd": _optional_path(materialization.effective_cwd),
        "provisioning_duration_seconds": (
            provisioning.duration_seconds if provisioning is not None else None
        ),
    }


def _optional_path(path: Path | None) -> str | None:
    return path.as_posix() if path is not None else None


def _add_reuse_payload(
    payload: dict[str, object],
    materialization: WorkspaceStateMaterializationRequest,
) -> None:
    if materialization.reuse is not None:
        payload["reuse"] = dict(materialization.reuse)
    workspace = payload["workspace"]
    if materialization.reuse_generation is not None and isinstance(workspace, dict):
        workspace["reuse_generation"] = materialization.reuse_generation


def _add_setup_payload(
    payload: dict[str, object],
    policy: WorkspaceSelectionRecord,
) -> None:
    if policy.setup is not None:
        payload["setup"] = {
            "profile_name": policy.setup.profile_name,
            "status": "pending",
            "commands": [
                command.model_dump(mode="json") for command in policy.setup.commands
            ],
        }


def _source_payload(
    source_ref: WorktreeSourceRef | None,
    source: WorkspaceSourceSnapshot,
    state_path: Path,
) -> dict[str, object]:
    if source_ref is None:
        return {
            "kind": "project",
            "node_id": None,
            "commit": source.run_base_commit,
            "tree": source.source_tree,
            "candidate_sequence": None,
        }
    payload: dict[str, object] = {
        "kind": source_ref.source_kind,
        "node_id": source_ref.source_node_id,
        "commit": source_ref.source_commit,
        "tree": source_ref.source_tree,
        "candidate_sequence": source_ref.candidate_sequence,
    }
    payload.update(_source_bundle_payload(source_ref, state_path))
    if source_ref.upstream_sources:
        payload["upstream_sources"] = [
            _source_payload(upstream, source, state_path)
            for upstream in source_ref.upstream_sources
        ]
    return payload


def _invocation_source_payload(
    source_ref: WorktreeSourceRef | None,
    source: WorkspaceSourceSnapshot,
    state_path: Path,
) -> dict[str, object]:
    if source_ref is None:
        return {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": source.run_base_commit,
            "source_tree": source.source_tree,
            "candidate_sequence": None,
        }
    payload: dict[str, object] = {
        "source_kind": source_ref.source_kind,
        "source_node_id": source_ref.source_node_id,
        "source_commit": source_ref.source_commit,
        "source_tree": source_ref.source_tree,
        "candidate_sequence": source_ref.candidate_sequence,
    }
    for key, value in _source_bundle_payload(source_ref, state_path).items():
        payload[f"source_{key}"] = value
    return payload


def _source_bundle_payload(
    source_ref: WorktreeSourceRef,
    state_path: Path,
) -> dict[str, object]:
    if source_ref.bundle_sha256 is None:
        return {}
    payload: dict[str, object] = {"bundle_sha256": source_ref.bundle_sha256}
    if source_ref.bundle_size_bytes is not None:
        payload["bundle_size_bytes"] = source_ref.bundle_size_bytes
    if source_ref.bundle_ref is not None:
        payload["bundle_ref"] = source_ref.bundle_ref
    if source_ref.bundle_path is not None:
        relative_path = _relative_source_bundle_path(source_ref.bundle_path, state_path)
        if relative_path is not None:
            payload["bundle_path"] = relative_path
    return payload


def _relative_source_bundle_path(bundle_path: Path, state_path: Path) -> str | None:
    try:
        return bundle_path.relative_to(state_path.parent.parent).as_posix()
    except ValueError:
        return None
