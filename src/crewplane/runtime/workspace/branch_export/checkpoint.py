from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from crewplane.artifacts.safe_files import contained_regular_file
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
    WorkspaceSourceSnapshot,
)
from crewplane.core.value_checks import is_strict_int
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.workspace.branch_export.fulfillment import (
    BranchExportCheckpoint,
)
from crewplane.runtime.workspace.state_selection import (
    required_lineage_state_path,
)
from crewplane.runtime.workspace.worktree.descriptors import (
    load_source_ref_from_state,
)
from crewplane.runtime.workspace.worktree.lineage import (
    ensure_source_commit_available,
    verify_source_commit_available,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from crewplane.version import SCHEMA_VERSION


class StageLookup(Protocol):
    def get_stage_dir(self, stage_name: str) -> Path | None: ...


def validated_checkpoint(
    plan: PreflightExecutionPlan,
    source: WorkspaceSourceSnapshot,
    node: PreflightExecutionNode,
    policy: WorkspaceSelectionRecord,
    stages_dir: Path,
    state_lookup: StageLookup,
    import_result_commit: bool,
) -> BranchExportCheckpoint:
    state_path = required_lineage_state_path(state_lookup, node.id)
    payload = _workspace_state_payload(state_path)
    _validate_state_header(plan, node, policy, payload)
    result = _mapping(payload.get("result"))
    refs = _mapping(payload.get("refs"))
    result_commit = _hex_object(result.get("result_commit"))
    result_tree = _hex_object(result.get("result_tree"))
    result_ref = _string(refs.get("result"))
    if result_commit is None or result_tree is None or result_ref is None:
        raise RuntimeError(
            f"Workspace branch export checkpoint is incomplete for node '{node.id}'."
        )
    source_ref = load_source_ref_from_state(state_path)
    bundle_path, bundle_relative_path, bundle_sha256, bundle_size_bytes = (
        _validated_bundle(
            stages_dir,
            payload,
        )
    )
    _reject_source_ref_mismatch(
        source_ref,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
        bundle_sha256,
        bundle_size_bytes,
    )
    if import_result_commit:
        _ensure_branch_export_source_available(source, source_ref)
    else:
        _verify_branch_export_source_available(source, source_ref)
    return BranchExportCheckpoint(
        state_path=state_path,
        state_relative_path=state_path.relative_to(stages_dir).as_posix(),
        node_id=node.id,
        task_id=_string(payload.get("task_id")) or "",
        result_commit=result_commit,
        result_tree=result_tree,
        result_ref=result_ref,
        bundle_path=bundle_path,
        bundle_relative_path=bundle_relative_path,
        bundle_sha256=bundle_sha256,
        bundle_size_bytes=bundle_size_bytes,
    )


def _ensure_branch_export_source_available(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
) -> None:
    try:
        ensure_source_commit_available(source, source_ref)
    except RuntimeError as exc:
        _raise_branch_export_result_mismatch(exc)
        raise


def _verify_branch_export_source_available(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
) -> None:
    try:
        verify_source_commit_available(source, source_ref)
    except RuntimeError as exc:
        _raise_branch_export_result_mismatch(exc)
        raise


def _raise_branch_export_result_mismatch(exc: RuntimeError) -> None:
    message = str(exc)
    if (
        "source tree mismatch" in message
        or "did not provide the expected commit" in message
    ):
        raise RuntimeError(
            "Workspace branch export bundle final result does not match the "
            "recorded commit and tree."
        ) from exc


def _workspace_state_payload(state_path: Path) -> dict[str, object]:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"Workspace branch export state is unreadable: {state_path.as_posix()}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Workspace branch export state is invalid: {state_path.as_posix()}"
        )
    return payload


def _validate_state_header(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    policy: WorkspaceSelectionRecord,
    payload: dict[str, object],
) -> None:
    workspace = _mapping(payload.get("workspace"))
    if not (
        payload.get("version") == SCHEMA_VERSION
        and payload.get("workflow_name") == plan.workflow_name
        and payload.get("workflow_signature") == plan.workflow_signature
        and payload.get("node_id") == node.id
        and payload.get("status") == "succeeded"
        and payload.get("role") == ProviderRole.EXECUTOR
        and payload.get("workspace_kind") == "worktree"
        and payload.get("logical_worktree_name") == policy.logical_worktree_name
        and payload.get("worktree_contract")
        == policy.worktree_contract.model_dump(mode="json")
        and workspace.get("materialization") == "worktree_checkout"
        and workspace.get("lineage_producer") is True
    ):
        raise RuntimeError(
            f"Workspace branch export state does not match node '{node.id}'."
        )


def _validated_bundle(
    stages_dir: Path,
    payload: dict[str, object],
) -> tuple[Path, str, str, int]:
    bundle = _mapping(payload.get("bundle"))
    relative_path = _string(bundle.get("path"))
    sha256 = _string(bundle.get("sha256"))
    size_bytes = bundle.get("size_bytes")
    if (
        relative_path is None
        or sha256 is None
        or not is_strict_int(size_bytes)
        or bundle.get("verified") is not True
    ):
        raise RuntimeError("Workspace branch export checkpoint lacks a bundle.")
    bundle_path = _contained_bundle_file(stages_dir, relative_path)
    if bundle_path is None:
        raise RuntimeError("Workspace branch export bundle is missing or unsafe.")
    actual_size, actual_sha256 = file_size_and_sha256(bundle_path)
    if actual_size != size_bytes:
        raise RuntimeError("Workspace branch export bundle size mismatch.")
    if actual_sha256 != sha256:
        raise RuntimeError("Workspace branch export bundle digest mismatch.")
    return bundle_path, relative_path, sha256, size_bytes


def _reject_source_ref_mismatch(
    source_ref: WorktreeSourceRef,
    result_commit: str,
    result_tree: str,
    result_ref: str,
    bundle_path: Path,
    bundle_sha256: str,
    bundle_size_bytes: int,
) -> None:
    if (
        source_ref.source_commit == result_commit
        and source_ref.source_tree == result_tree
        and source_ref.bundle_ref == result_ref
        and source_ref.bundle_path == bundle_path
        and source_ref.bundle_sha256 == bundle_sha256
        and source_ref.bundle_size_bytes == bundle_size_bytes
    ):
        return
    message = "Workspace branch export checkpoint source descriptor mismatch."
    raise RuntimeError(message)


def _contained_bundle_file(stages_dir: Path, relative_path: str) -> Path | None:
    direct = contained_regular_file(stages_dir, relative_path)
    if direct is not None:
        return direct
    path = Path(relative_path)
    parts = path.parts
    try:
        index = parts.index("execution-stages")
    except ValueError:
        return None
    if len(parts) <= index + 2 or parts[index + 1] != stages_dir.name:
        return None
    return contained_regular_file(stages_dir, Path(*parts[index + 2 :]).as_posix())


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _hex_object(value: object) -> str | None:
    if (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(char in "0123456789abcdef" for char in value)
    ):
        return value
    return None
