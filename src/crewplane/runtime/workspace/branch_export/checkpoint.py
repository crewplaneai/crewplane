from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.architecture.safe_files import contained_regular_file
from crewplane.artifacts.workspace.state.contracts import (
    require_workspace_state_contract,
)
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
    WorkspaceSourceSnapshot,
)
from crewplane.core.value_checks import is_strict_int
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workspace.git_policy import is_git_object_id
from crewplane.core.workspace.repository_identity import workspace_repository_id
from crewplane.runtime.workspace.branch_export.fulfillment import (
    BranchExportCheckpoint,
)
from crewplane.runtime.workspace.git import git
from crewplane.runtime.workspace.state_selection import (
    required_lineage_state_path,
)
from crewplane.runtime.workspace.worktree.descriptors import (
    load_source_ref_from_state,
)
from crewplane.runtime.workspace.worktree.lineage import (
    verify_source_commit_available,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from crewplane.version import SCHEMA_VERSION


class StageLookup(Protocol):
    def get_node_dir(self, request: NodeArtifactRequest) -> Path | None: ...


def validated_checkpoint(
    plan: PreflightExecutionPlan,
    source: WorkspaceSourceSnapshot,
    node: PreflightExecutionNode,
    policy: WorkspaceSelectionRecord,
    stages_dir: Path,
    state_lookup: StageLookup,
    run_id: str,
    run_key_name: str,
) -> BranchExportCheckpoint:
    state_path = required_lineage_state_path(state_lookup, node)
    payload = _workspace_state_payload(state_path)
    require_workspace_state_contract(payload, "export")
    _validate_state_header(
        plan,
        source,
        node,
        policy,
        payload,
        run_id,
        run_key_name,
    )
    result = _mapping(payload.get("result"))
    refs = _mapping(payload.get("refs"))
    result_commit = result.get("result_commit")
    result_tree = result.get("result_tree")
    result_ref = _string(refs.get("result"))
    if (
        not is_git_object_id(result_commit)
        or not is_git_object_id(result_tree)
        or result_ref is None
    ):
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
        or "result tree mismatch" in message
        or "result is not a commit" in message
        or "unexpected parent" in message
    ):
        raise RuntimeError(
            "Workspace branch export bundle final result does not match the "
            "recorded commit and tree."
        ) from exc
    if message.startswith("Workspace lineage source verification failed"):
        raise RuntimeError(message) from exc
    raise RuntimeError(
        "Workspace lineage source verification failed while validating recorded "
        f"Git artifacts: {message}"
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
    source: WorkspaceSourceSnapshot,
    node: PreflightExecutionNode,
    policy: WorkspaceSelectionRecord,
    payload: dict[str, object],
    run_id: str,
    run_key_name: str,
) -> None:
    workspace = _mapping(payload.get("workspace"))
    git_payload = _mapping(payload.get("git"))
    if not (
        payload.get("version") == SCHEMA_VERSION
        and payload.get("run_id") == run_id
        and payload.get("run_key_name") == run_key_name
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
        and git_payload.get("repo_id") == source.repository_id
    ):
        raise RuntimeError(
            f"Workspace branch export state does not match node '{node.id}'."
        )
    _require_current_repository_identity(source)


def _require_current_repository_identity(source: WorkspaceSourceSnapshot) -> None:
    repo_root = Path(source.git_top_level)
    command = git(repo_root)
    common_git_dir = _resolved_git_path(
        repo_root,
        command.text("rev-parse", "--git-common-dir"),
    )
    object_format = command.text("rev-parse", "--show-object-format=storage")
    current_repository_id = workspace_repository_id(
        common_git_dir,
        repo_root / source.project_root_relative_path,
        object_format,
    )
    if (
        common_git_dir != Path(source.common_git_dir).resolve(strict=False)
        or current_repository_id != source.repository_id
    ):
        raise RuntimeError("Workspace branch export repository identity changed.")


def _resolved_git_path(repo_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve(strict=False)


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
