from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
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

from .lineage_discard import apply_lineage_discard
from .running_state_payload import build_running_workspace_state_payload

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
    payload = build_running_workspace_state_payload(
        state_path,
        request,
        node,
        source,
        policy,
        materialization,
        datetime.now(UTC).isoformat(),
    )
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
_TERMINAL_WORKSPACE_STATUSES = frozenset({"cancelled", "failed", "succeeded"})


def require_workspace_state_payload_identity(
    current: Mapping[str, object],
    expected: Mapping[str, object],
) -> None:
    terminalized = (
        current.get("status") in _TERMINAL_WORKSPACE_STATUSES
        and expected.get("status") in _TERMINAL_WORKSPACE_STATUSES
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


@contextmanager
def edit_workspace_state(state_path: Path) -> Iterator[dict[str, object]]:
    """Persist successful workspace state edits atomically under the state lock."""

    with _state_lock(state_path):
        payload = read_workspace_state(state_path)
        yield payload
        payload["updated_at"] = datetime.now(UTC).isoformat()
        atomic_write_json(state_path, payload)


def mutate_workspace_state(
    state_path: Path,
    mutation: Callable[[dict[str, object]], None],
) -> dict[str, object]:
    with edit_workspace_state(state_path) as payload:
        mutation(payload)
    return payload


def update_workspace_state(
    state_path: Path,
    request: WorkspaceStateUpdateRequest,
) -> None:
    mutate_workspace_state(
        state_path,
        partial(_apply_workspace_state_update, request=request),
    )


def _apply_workspace_state_update(
    payload: dict[str, object],
    request: WorkspaceStateUpdateRequest,
) -> None:
    previous_status = payload.get("status")
    _require_valid_terminal_transition(previous_status, request.status)
    payload["status"] = request.status
    _apply_workspace_retention(payload, request.retention)
    if request.diagnostics is not None:
        payload["diagnostics"] = request.diagnostics
    _apply_terminal_evidence(payload, request, previous_status)
    if request.setup is not None:
        payload["setup"] = dict(request.setup)
    _apply_child_environment_status(payload, request, previous_status)


def _require_valid_terminal_transition(
    previous_status: object,
    requested_status: str,
) -> None:
    if (
        previous_status in _TERMINAL_WORKSPACE_STATUSES
        and requested_status != previous_status
    ):
        raise RuntimeError(
            "Workspace terminal outcome cannot be rewritten from "
            f"{previous_status!r} to {requested_status!r}."
        )


def _apply_workspace_retention(
    payload: dict[str, object],
    retention: WorkspaceStateRetention,
) -> None:
    workspace = payload.get("workspace")
    if isinstance(workspace, dict):
        workspace["retention"] = retention.retention
        workspace["retained_reason"] = retention.retained_reason


def _apply_terminal_evidence(
    payload: dict[str, object],
    request: WorkspaceStateUpdateRequest,
    previous_status: object,
) -> None:
    terminalized = previous_status in _TERMINAL_WORKSPACE_STATUSES
    _apply_evidence_field(payload, "result", request.result, terminalized)
    _apply_evidence_field(payload, "refs", request.refs, terminalized)
    _apply_evidence_field(payload, "bundle", request.bundle, terminalized)


def _apply_evidence_field(
    payload: dict[str, object],
    field_name: Literal["result", "refs", "bundle"],
    evidence: Mapping[str, object] | None,
    terminalized: bool,
) -> None:
    if evidence is None:
        return
    if terminalized and payload.get(field_name) != dict(evidence):
        evidence_name = "ref" if field_name == "refs" else field_name
        raise RuntimeError(f"Workspace terminal {evidence_name} evidence is immutable.")
    payload[field_name] = dict(evidence)


def _apply_child_environment_status(
    payload: dict[str, object],
    request: WorkspaceStateUpdateRequest,
    previous_status: object,
) -> None:
    environment = payload.get("child_process_environment")
    if not isinstance(environment, dict) or environment.get("required") is not True:
        return
    if request.child_environment_applied is not None:
        environment["applied"] = request.child_environment_applied
    elif request.status == "succeeded" and previous_status == "running":
        environment["applied"] = True


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
