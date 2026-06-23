from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

from crewplane.architecture.contracts import JsonObject
from crewplane.artifacts.atomic import atomic_write_json
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    WorkspaceSelectionRecord,
    WorkspaceSourceSnapshot,
)
from crewplane.version import SCHEMA_VERSION

from .worktree import WorktreeSourceRef


class RenderedWorkspaceFileDescriptor(TypedDict):
    occurrence_id: str
    invocation_id: str
    role: str
    round_num: int | None
    audit_round_num: int | None
    source_kind: str | None
    source_node_id: str | None
    source_commit: str | None
    source_tree: str | None
    candidate_sequence: int | None
    workspace_relative_path: str
    git_blob: str | None
    git_file_mode: str | None
    byte_size: int
    canonical_blob_sha256: str
    injected_sha256: str
    byte_source: str
    literal_path_verified: bool
    utf8_validated: bool
    target: str


@dataclass(frozen=True)
class WorkspaceStateWriteRequest:
    run_id: str
    run_key_name: str
    workflow_name: str
    workflow_signature: str
    task_id: str
    provider: str
    role_label: str
    round_num: int
    audit_round_num: int | None
    invoker: JsonObject
    rendered_workspace_files: tuple[RenderedWorkspaceFileDescriptor, ...] = ()


@dataclass(frozen=True)
class WorkspaceProvisioningMetadata:
    checkout_size_bytes: int
    duration_seconds: float


@dataclass(frozen=True)
class WorkspaceStateMaterializationRequest:
    workspace_path: Path
    child_environment_required: bool
    cache_root: str | None = None
    effective_cwd: Path | None = None
    checkout_root: Path | None = None
    provisioning: WorkspaceProvisioningMetadata | None = None
    source_ref: WorktreeSourceRef | None = None
    materialization: str = "snapshot_checkout"
    writable: bool = True
    lineage_producer: bool = False
    worktree_lock_mode: str | None = None
    reuse: Mapping[str, object] | None = None


@dataclass(frozen=True)
class WorkspaceStateRetention:
    retention: str = "retained"
    retained_reason: str | None = None


@dataclass(frozen=True)
class WorkspaceStateUpdateRequest:
    status: Literal["succeeded", "failed", "cancelled"]
    diagnostics: list[dict[str, str]] | None = None
    retention: WorkspaceStateRetention = field(default_factory=WorkspaceStateRetention)
    result: Mapping[str, object] | None = None
    refs: Mapping[str, object] | None = None
    bundle: Mapping[str, object] | None = None
    setup: Mapping[str, object] | None = None
    child_environment_applied: bool | None = None
    base_payload: Mapping[str, object] | None = None


def write_running_workspace_state(
    state_path: Path,
    request: WorkspaceStateWriteRequest,
    node: PreflightExecutionNode,
    source: WorkspaceSourceSnapshot,
    policy: WorkspaceSelectionRecord,
    materialization: WorkspaceStateMaterializationRequest,
) -> None:
    invocation_source = materialization.source_ref or WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=source.run_base_commit,
        source_tree=source.source_tree,
        candidate_sequence=None,
    )
    payload = {
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
        "git": {
            "object_format": source.object_format,
            "repo_id": source.repository_id,
            "run_base_commit": source.run_base_commit,
            "source_tree": source.source_tree,
            "worktree_config_active": False,
            "worktree_lock_mode": materialization.worktree_lock_mode,
        },
        "source": _source_payload(invocation_source, state_path),
        "workspace": {
            "path": None,
            "effective_cwd": None,
            "cache_key": materialization.workspace_path.name,
            "materialization": materialization.materialization,
            "writable": materialization.writable,
            "lineage_producer": materialization.lineage_producer,
            "retention": "pending",
            "retained_reason": None,
            "project_root_relative_path": source.project_root_relative_path,
        },
        "execution": {
            "cache_root": materialization.cache_root,
            "workspace_path": materialization.workspace_path.as_posix(),
            "checkout_root": (
                materialization.checkout_root.as_posix()
                if materialization.checkout_root is not None
                else None
            ),
            "checkout_size_bytes": (
                materialization.provisioning.checkout_size_bytes
                if materialization.provisioning is not None
                else None
            ),
            "effective_cwd": (
                materialization.effective_cwd.as_posix()
                if materialization.effective_cwd is not None
                else None
            ),
            "provisioning_duration_seconds": (
                materialization.provisioning.duration_seconds
                if materialization.provisioning is not None
                else None
            ),
        },
        "invocation_source": _invocation_source_payload(invocation_source, state_path),
        "child_process_environment": {
            "required": materialization.child_environment_required,
            "applied": False if materialization.child_environment_required else None,
        },
        "invoker": request.invoker,
        "rendered_workspace_files": list(request.rendered_workspace_files),
        "diagnostics": [],
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if materialization.reuse is not None:
        payload["reuse"] = dict(materialization.reuse)
    if policy.setup is not None:
        payload["setup"] = {
            "profile_name": policy.setup.profile_name,
            "status": "pending",
            "commands": [
                command.model_dump(mode="json") for command in policy.setup.commands
            ],
        }
    atomic_write_json(state_path, payload)


def _source_payload(
    source_ref: WorktreeSourceRef, state_path: Path
) -> dict[str, object]:
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
            _source_payload(upstream, state_path)
            for upstream in source_ref.upstream_sources
        ]
    return payload


def _invocation_source_payload(
    source_ref: WorktreeSourceRef,
    state_path: Path,
) -> dict[str, object]:
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


def update_workspace_state(
    state_path: Path,
    request: WorkspaceStateUpdateRequest,
) -> None:
    payload = (
        deepcopy(dict(request.base_payload))
        if request.base_payload is not None
        else json.loads(state_path.read_text(encoding="utf-8"))
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid workspace state: {state_path.as_posix()}")
    previous_status = payload.get("status")
    payload["status"] = request.status
    payload["updated_at"] = datetime.now(UTC).isoformat()
    workspace = payload.get("workspace")
    if isinstance(workspace, dict):
        workspace["retention"] = request.retention.retention
        workspace["retained_reason"] = request.retention.retained_reason
    if request.diagnostics is not None:
        payload["diagnostics"] = request.diagnostics
    if request.result is not None:
        payload["result"] = dict(request.result)
    if request.refs is not None:
        payload["refs"] = dict(request.refs)
    if request.bundle is not None:
        payload["bundle"] = dict(request.bundle)
    if request.setup is not None:
        payload["setup"] = dict(request.setup)
    env = payload.get("child_process_environment")
    if isinstance(env, dict) and env.get("required") is True:
        if request.child_environment_applied is not None:
            env["applied"] = request.child_environment_applied
        elif request.status == "succeeded" and previous_status == "running":
            env["applied"] = True
    atomic_write_json(state_path, payload)


def update_workspace_setup(
    state_path: Path,
    setup: dict[str, object],
    base_payload: Mapping[str, object] | None = None,
) -> None:
    payload = (
        deepcopy(dict(base_payload))
        if base_payload is not None
        else json.loads(state_path.read_text(encoding="utf-8"))
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid workspace state: {state_path.as_posix()}")
    payload["setup"] = setup
    payload["updated_at"] = datetime.now(UTC).isoformat()
    atomic_write_json(state_path, payload)


def discard_workspace_lineage(
    state_path: Path,
    reason: str,
) -> None:
    if not state_path.is_file():
        return
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid workspace state: {state_path.as_posix()}")
    workspace = payload.get("workspace")
    if isinstance(workspace, dict):
        workspace["lineage_producer"] = False
    result = payload.get("result")
    if isinstance(result, dict):
        sanitized_result: dict[str, object] = {
            "lineage_produced": False,
            "lineage_discarded": True,
            "lineage_discard_reason": reason,
        }
        changed_path_count = result.get("changed_path_count")
        if isinstance(changed_path_count, int) and not isinstance(
            changed_path_count, bool
        ):
            sanitized_result["changed_path_count"] = changed_path_count
        final_head = result.get("final_head")
        if final_head is None:
            final_head = result.get("result_commit") or result.get("candidate_commit")
        if isinstance(final_head, str):
            sanitized_result["final_head"] = final_head
        payload["result"] = sanitized_result
    else:
        payload["result"] = {
            "lineage_produced": False,
            "lineage_discarded": True,
            "lineage_discard_reason": reason,
        }
    payload.pop("refs", None)
    payload.pop("bundle", None)
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, list):
        diagnostics = []
    diagnostics.append(
        {
            "level": "warning",
            "message": f"Workspace lineage discarded: {reason}",
        }
    )
    payload["diagnostics"] = diagnostics
    payload["updated_at"] = datetime.now(UTC).isoformat()
    atomic_write_json(state_path, payload)
