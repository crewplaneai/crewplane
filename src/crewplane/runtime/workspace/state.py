from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, RLock
from typing import TYPE_CHECKING, Literal, TypedDict

from crewplane.architecture.contracts import JsonObject
from crewplane.artifacts.atomic import atomic_write_json
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    WorkspaceSelectionRecord,
    WorkspaceSourceSnapshot,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.version import SCHEMA_VERSION

from . import state_evidence as _state_evidence
from .lineage_discard import apply_lineage_discard

mark_workspace_temporary_ref_removed = (
    _state_evidence.mark_workspace_temporary_ref_removed
)
record_workspace_process_drain = _state_evidence.record_workspace_process_drain
record_workspace_temporary_ref = _state_evidence.record_workspace_temporary_ref
update_workspace_ref_publication = _state_evidence.update_workspace_ref_publication
update_workspace_ref_publication_phase = (
    _state_evidence.update_workspace_ref_publication_phase
)
update_workspace_setup = _state_evidence.update_workspace_setup

if TYPE_CHECKING:
    from .worktree.types import WorktreeSourceRef


class RenderedWorkspaceFileDescriptor(TypedDict):
    occurrence_id: str
    invocation_id: str
    role: ProviderRole
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
    role_label: ProviderRole
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
    worktree_git_dir: Path | None = None
    provisioning: WorkspaceProvisioningMetadata | None = None
    source_ref: WorktreeSourceRef | None = None
    materialization: str = "snapshot_checkout"
    writable: bool = True
    lineage_producer: bool = False
    worktree_lock_mode: str | None = None
    reuse: Mapping[str, object] | None = None
    reuse_generation: int | None = None


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
    from .worktree.types import WorktreeSourceRef

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
            "git_top_level": source.git_top_level,
            "active_git_dir": source.active_git_dir,
            "common_git_dir": source.common_git_dir,
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
            "worktree_git_dir": (
                materialization.worktree_git_dir.as_posix()
                if materialization.worktree_git_dir is not None
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
        "process_drain": {"status": "not_started"},
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if materialization.reuse is not None:
        payload["reuse"] = dict(materialization.reuse)
    workspace = payload["workspace"]
    if materialization.reuse_generation is not None and isinstance(workspace, dict):
        workspace["reuse_generation"] = materialization.reuse_generation
    if policy.setup is not None:
        payload["setup"] = {
            "profile_name": policy.setup.profile_name,
            "status": "pending",
            "commands": [
                command.model_dump(mode="json") for command in policy.setup.commands
            ],
        }
    with _state_lock(state_path):
        if state_path.exists():
            current = read_workspace_state(state_path)
            if current.get("status") in {"succeeded", "failed", "cancelled"}:
                raise RuntimeError(
                    "Workspace materialization cannot rewrite a terminal outcome."
                )
            if any(
                current.get(field) != payload.get(field)
                for field in (
                    "version",
                    "run_id",
                    "run_key_name",
                    "workflow_name",
                    "workflow_signature",
                    "node_id",
                    "task_id",
                    "provider",
                    "role",
                    "round_num",
                    "audit_round_num",
                )
            ):
                raise RuntimeError(
                    "Workspace materialization state identity is contradictory."
                )
            current.update(payload)
            payload = current
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


_STATE_LOCKS_GUARD = Lock()
_STATE_LOCKS: dict[Path, RLock] = {}


def _state_lock(state_path: Path) -> RLock:
    key = state_path.resolve(strict=False)
    with _STATE_LOCKS_GUARD:
        lock = _STATE_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _STATE_LOCKS[key] = lock
        return lock


def read_workspace_state(state_path: Path) -> dict[str, object]:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Invalid workspace state: {state_path.as_posix()}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid workspace state: {state_path.as_posix()}")
    return payload


def require_workspace_state_identity(
    state_path: Path,
    expected: Mapping[str, object],
) -> None:
    current = read_workspace_state(state_path)
    require_workspace_state_payload_identity(current, expected)


_RUNTIME_MUTABLE_WORKSPACE_STATE_FIELDS = frozenset(
    {"process_drain", "updated_at", "workspace_mutator"}
)
_TERMINAL_WORKSPACE_STATE_FIELDS = frozenset(
    {
        "branch_export",
        "bundle",
        "diagnostics",
        "ref_publication",
        "refs",
        "result",
        "status",
        "temporary_refs",
    }
)


def require_workspace_state_payload_identity(
    current: Mapping[str, object],
    expected: Mapping[str, object],
) -> None:
    terminal_statuses = {"cancelled", "failed", "succeeded"}
    terminalized = (
        current.get("status") in terminal_statuses
        and expected.get("status") in terminal_statuses
    )
    if _workspace_state_identity(current, terminalized) != _workspace_state_identity(
        expected,
        terminalized,
    ):
        raise RuntimeError(
            "Workspace state identity changed during execution; the workspace "
            "was retained."
        )


def _workspace_state_identity(
    payload: Mapping[str, object],
    terminalized: bool,
) -> dict[str, object]:
    mutable_fields = _RUNTIME_MUTABLE_WORKSPACE_STATE_FIELDS
    if terminalized:
        mutable_fields |= _TERMINAL_WORKSPACE_STATE_FIELDS
    identity = {
        field: value for field, value in payload.items() if field not in mutable_fields
    }
    if not terminalized:
        return identity
    workspace = payload.get("workspace")
    if isinstance(workspace, Mapping):
        identity["workspace"] = {
            field: value
            for field, value in workspace.items()
            if field not in {"retained_reason", "retention"}
        }
    child_environment = payload.get("child_process_environment")
    if isinstance(child_environment, Mapping):
        identity["child_process_environment"] = {
            field: value
            for field, value in child_environment.items()
            if field != "applied"
        }
    return identity


def mutate_workspace_state(
    state_path: Path,
    mutation: Callable[[dict[str, object]], None],
) -> dict[str, object]:
    with _state_lock(state_path):
        payload = read_workspace_state(state_path)
        mutation(payload)
        payload["updated_at"] = datetime.now(UTC).isoformat()
        atomic_write_json(state_path, payload)
        return payload


def update_workspace_state(
    state_path: Path,
    request: WorkspaceStateUpdateRequest,
) -> None:
    def apply_update(payload: dict[str, object]) -> None:
        previous_status = payload.get("status")
        if (
            previous_status in {"succeeded", "failed", "cancelled"}
            and request.status != previous_status
        ):
            raise RuntimeError(
                "Workspace terminal outcome cannot be rewritten from "
                f"{previous_status!r} to {request.status!r}."
            )
        payload["status"] = request.status
        workspace = payload.get("workspace")
        if isinstance(workspace, dict):
            workspace["retention"] = request.retention.retention
            workspace["retained_reason"] = request.retention.retained_reason
        if request.diagnostics is not None:
            payload["diagnostics"] = request.diagnostics
        if request.result is not None:
            if previous_status in {"succeeded", "failed", "cancelled"} and (
                payload.get("result") != dict(request.result)
            ):
                raise RuntimeError("Workspace terminal result evidence is immutable.")
            payload["result"] = dict(request.result)
        if request.refs is not None:
            if previous_status in {"succeeded", "failed", "cancelled"} and (
                payload.get("refs") != dict(request.refs)
            ):
                raise RuntimeError("Workspace terminal ref evidence is immutable.")
            payload["refs"] = dict(request.refs)
        if request.bundle is not None:
            if previous_status in {"succeeded", "failed", "cancelled"} and (
                payload.get("bundle") != dict(request.bundle)
            ):
                raise RuntimeError("Workspace terminal bundle evidence is immutable.")
            payload["bundle"] = dict(request.bundle)
        if request.setup is not None:
            payload["setup"] = dict(request.setup)
        env = payload.get("child_process_environment")
        if isinstance(env, dict) and env.get("required") is True:
            if request.child_environment_applied is not None:
                env["applied"] = request.child_environment_applied
            elif request.status == "succeeded" and previous_status == "running":
                env["applied"] = True

    mutate_workspace_state(state_path, apply_update)


def update_workspace_retention(
    state_path: Path,
    retention: WorkspaceStateRetention,
    diagnostic: Mapping[str, str] | None = None,
) -> None:
    def apply_retention(payload: dict[str, object]) -> None:
        if payload.get("status") not in {"succeeded", "failed", "cancelled"}:
            raise RuntimeError(
                "Workspace retention can change only after terminal outcome publication."
            )
        workspace = payload.get("workspace")
        if not isinstance(workspace, dict):
            raise RuntimeError("Workspace state lacks workspace retention evidence.")
        if diagnostic is not None:
            diagnostics = payload.get("diagnostics")
            if not isinstance(diagnostics, list):
                raise RuntimeError("Workspace state lacks diagnostic evidence.")
            diagnostics.append(dict(diagnostic))
        workspace["retention"] = retention.retention
        workspace["retained_reason"] = retention.retained_reason

    mutate_workspace_state(state_path, apply_retention)


def discard_workspace_lineage(
    state_path: Path,
    reason: str,
) -> None:
    if not state_path.is_file():
        return

    mutate_workspace_state(
        state_path,
        lambda payload: apply_lineage_discard(payload, reason),
    )
