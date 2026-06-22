from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceFileLocator,
)
from orchestrator_cli.core.preflight.workspace_git_file_reads import (
    SUPPORTED_FILE_MODES,
    git_cat_blob,
    git_ls_tree,
    valid_utf8_without_nul,
)
from orchestrator_cli.runtime.workspace.state import RenderedWorkspaceFileDescriptor
from orchestrator_cli.runtime.workspace.state_selection import (
    latest_executor_lineage_state_path,
    required_lineage_state_path,
    review_loop_canonical_lineage_state_path,
    same_node_executor_state_path,
)
from orchestrator_cli.runtime.workspace.worktree import (
    WorktreeSourceRef,
    ensure_source_commit_available,
)
from orchestrator_cli.runtime.workspace.worktree_descriptors import (
    load_source_ref_from_state,
)


@dataclass(frozen=True)
class ResolvedWorkspaceFile:
    locator: WorkspaceFileLocator
    text: str
    byte_size: int
    sha256: str
    source_ref: WorktreeSourceRef | None = None
    git_blob: str | None = None
    git_file_mode: str | None = None
    literal_path_verified: bool = False
    utf8_validated: bool = False


@dataclass(frozen=True)
class WorkspaceCandidateSourceContext:
    role_label: str
    round_num: int
    audit_round_num: int | None


def resolve_project_initial_workspace_file(
    plan: PreflightExecutionPlan,
    locator_id: str,
) -> ResolvedWorkspaceFile:
    locator = workspace_file_locator(plan, locator_id)
    if locator.source_class != "project_initial":
        raise RuntimeError(
            "Runtime-dynamic workspace file locator resolution is unavailable in "
            f"this build: {locator_id}."
        )
    if locator.content_ref is None:
        raise RuntimeError(
            f"Workspace file locator is missing preflight content: {locator_id}."
        )
    payload = _read_preflight_workspace_file(plan, locator.content_ref)
    digest = hashlib.sha256(payload).hexdigest()
    if (
        locator.canonical_blob_sha256 is not None
        and digest != locator.canonical_blob_sha256
    ):
        raise RuntimeError(
            f"Workspace file locator content digest mismatch: {locator_id}."
        )
    if locator.byte_size is not None and len(payload) != locator.byte_size:
        raise RuntimeError(
            f"Workspace file locator content size mismatch: {locator_id}."
        )
    text = _decode_workspace_file(locator_id, payload)
    return ResolvedWorkspaceFile(
        locator=locator,
        text=text,
        byte_size=len(payload),
        sha256=digest,
        source_ref=_project_source_ref(plan),
        git_blob=locator.git_blob,
        git_file_mode=locator.git_file_mode,
        literal_path_verified=locator.literal_path_verified,
        utf8_validated=locator.utf8_validated,
    )


def resolve_workspace_file(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    locator_id: str,
    workspace_candidate_source: bool = False,
    workspace_candidate_context: WorkspaceCandidateSourceContext | None = None,
) -> ResolvedWorkspaceFile:
    locator = workspace_file_locator(plan, locator_id)
    if locator.source_class == "project_initial" and not _uses_candidate_source(
        locator,
        workspace_candidate_source,
    ):
        return resolve_project_initial_workspace_file(plan, locator_id)
    source = dynamic_locator_source(
        plan,
        output,
        locator,
        workspace_candidate_source,
        workspace_candidate_context,
    )
    payload, git_blob, git_file_mode = read_dynamic_locator_blob(plan, locator, source)
    digest = hashlib.sha256(payload).hexdigest()
    text = _decode_workspace_file(locator_id, payload)
    return ResolvedWorkspaceFile(
        locator=locator,
        text=text,
        byte_size=len(payload),
        sha256=digest,
        source_ref=source,
        git_blob=git_blob,
        git_file_mode=git_file_mode,
        literal_path_verified=True,
        utf8_validated=True,
    )


def workspace_file_locator(
    plan: PreflightExecutionPlan,
    locator_id: str,
) -> WorkspaceFileLocator:
    for locator in plan.workspace_file_locators:
        if locator.locator_id == locator_id:
            return locator
    raise RuntimeError(f"Workspace file locator not found in plan: {locator_id}.")


def dynamic_locator_source(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    locator: WorkspaceFileLocator,
    workspace_candidate_source: bool = False,
    workspace_candidate_context: WorkspaceCandidateSourceContext | None = None,
) -> WorktreeSourceRef:
    state_path = dynamic_locator_source_state_path(
        plan,
        output,
        locator,
        workspace_candidate_source,
        workspace_candidate_context,
    )
    if _uses_candidate_source(locator, workspace_candidate_source):
        return candidate_source_ref_from_state(state_path)
    return load_source_ref_from_state(state_path)


def dynamic_locator_source_state_path(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    locator: WorkspaceFileLocator,
    workspace_candidate_source: bool = False,
    workspace_candidate_context: WorkspaceCandidateSourceContext | None = None,
) -> Path:
    node = next((item for item in plan.nodes if item.id == locator.node_id), None)
    if node is None or node.workspace_policy is None:
        raise RuntimeError(
            "Runtime-dynamic workspace file locator has no workspace policy: "
            f"{locator.locator_id}."
        )
    if _uses_candidate_source(locator, workspace_candidate_source):
        contextual_state_path = _contextual_candidate_source_state_path(
            output,
            node,
            workspace_candidate_context,
        )
        if contextual_state_path is not None:
            return contextual_state_path
        if workspace_candidate_context is not None:
            raise RuntimeError(
                "Workspace source node has no matching executor state for "
                f"{workspace_candidate_context.role_label} round "
                f"{workspace_candidate_context.round_num}: {locator.node_id}."
            )
        stage_dir = output.get_stage_dir(locator.node_id)
        if stage_dir is None:
            raise RuntimeError(
                f"Workspace source node has no stage directory: {locator.node_id}."
            )
        state_path = review_loop_canonical_lineage_state_path(
            stage_dir, locator.node_id
        ) or latest_executor_lineage_state_path(stage_dir)
        if state_path is None:
            raise RuntimeError(
                "Workspace source node has no succeeded executor state: "
                f"{locator.node_id}."
            )
    elif (
        node.workspace_policy.source_kind == "node"
        and node.workspace_policy.source_node_id is not None
    ):
        state_path = required_lineage_state_path(
            output,
            node.workspace_policy.source_node_id,
        )
    else:
        raise RuntimeError(
            "Runtime-dynamic workspace file locator has no available candidate "
            f"source: {locator.locator_id}."
        )
    return state_path


def _contextual_candidate_source_state_path(
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    context: WorkspaceCandidateSourceContext | None,
) -> Path | None:
    if context is None:
        return None
    if context.role_label == "reviewer":
        return same_node_executor_state_path(
            output,
            node,
            context.round_num,
            context.audit_round_num,
            allow_prior_fallback=_reviewer_allows_prior_fallback(context),
        )
    if context.role_label == "executor" and context.round_num > 1:
        return same_node_executor_state_path(
            output,
            node,
            context.round_num - 1,
            context.audit_round_num,
            allow_prior_fallback=True,
        )
    return None


def _reviewer_allows_prior_fallback(context: WorkspaceCandidateSourceContext) -> bool:
    return context.audit_round_num is not None and context.audit_round_num > 1


def _uses_candidate_source(
    locator: WorkspaceFileLocator,
    workspace_candidate_source: bool,
) -> bool:
    if locator.target == "reviewer_prompt":
        return locator.source_class == "runtime_dynamic"
    return workspace_candidate_source and locator.runtime_dynamic_after_candidate


def read_dynamic_locator_blob(
    plan: PreflightExecutionPlan,
    locator: WorkspaceFileLocator,
    source: WorktreeSourceRef,
) -> tuple[bytes, str, str]:
    if plan.workspace_source is None:
        raise RuntimeError(
            f"Workspace file locator has no source snapshot: {locator.locator_id}."
        )
    ensure_source_commit_available(plan.workspace_source, source)
    record = git_ls_tree(
        plan.workspace_source.git_top_level,
        source.source_commit,
        locator.git_top_relative_path,
    )
    if record is None or record.path != locator.git_top_relative_path:
        raise RuntimeError(
            "Runtime-dynamic workspace file locator does not resolve exactly: "
            f"{locator.locator_id}."
        )
    if record.object_type != "blob" or record.mode not in SUPPORTED_FILE_MODES:
        raise RuntimeError(
            "Runtime-dynamic workspace file locator must resolve to a regular "
            f"Git blob: {locator.locator_id}."
        )
    payload = git_cat_blob(plan.workspace_source.git_top_level, record.object_id)
    if not valid_utf8_without_nul(payload):
        raise RuntimeError(
            "Runtime-dynamic workspace file locator content must be UTF-8 text "
            f"without NUL bytes: {locator.locator_id}."
        )
    return payload, record.object_id, record.mode


def rendered_workspace_file_descriptor(
    resolved_file: ResolvedWorkspaceFile,
    node_id: str,
    task_id: str,
    role: str,
    round_num: int,
    audit_round_num: int | None,
) -> RenderedWorkspaceFileDescriptor:
    locator = resolved_file.locator
    source_ref = resolved_file.source_ref
    return {
        "occurrence_id": locator.occurrence_id,
        "invocation_id": rendered_workspace_file_invocation_id(
            node_id,
            task_id,
            role,
            round_num,
            audit_round_num,
        ),
        "role": role,
        "round_num": round_num,
        "audit_round_num": audit_round_num,
        "source_kind": source_ref.source_kind if source_ref is not None else None,
        "source_node_id": source_ref.source_node_id if source_ref is not None else None,
        "source_commit": source_ref.source_commit if source_ref is not None else None,
        "source_tree": source_ref.source_tree if source_ref is not None else None,
        "candidate_sequence": (
            source_ref.candidate_sequence if source_ref is not None else None
        ),
        "workspace_relative_path": locator.workspace_relative_path,
        "git_blob": resolved_file.git_blob,
        "git_file_mode": resolved_file.git_file_mode,
        "byte_size": resolved_file.byte_size,
        "canonical_blob_sha256": resolved_file.sha256,
        "injected_sha256": resolved_file.sha256,
        "byte_source": "git_blob",
        "literal_path_verified": resolved_file.literal_path_verified,
        "utf8_validated": resolved_file.utf8_validated,
        "target": locator.target,
    }


def rendered_workspace_file_invocation_id(
    node_id: str,
    task_id: str,
    role: str,
    round_num: int,
    audit_round_num: int | None,
) -> str:
    audit = f".audit-{audit_round_num}" if audit_round_num is not None else ""
    return f"{node_id}.{role}.{task_id}{audit}.round-{round_num}"


def _project_source_ref(plan: PreflightExecutionPlan) -> WorktreeSourceRef | None:
    if plan.workspace_source is None:
        return None
    return WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
        candidate_sequence=None,
    )


def required_workspace_state(
    output: ArtifactStorePort,
    node_id: str,
) -> dict[str, object]:
    return load_workspace_state(required_lineage_state_path(output, node_id))


def latest_executor_workspace_state(
    output: ArtifactStorePort,
    node_id: str,
) -> dict[str, object]:
    stage_dir = output.get_stage_dir(node_id)
    if stage_dir is None:
        raise RuntimeError(f"Workspace source node has no stage directory: {node_id}.")
    state_path = latest_executor_lineage_state_path(stage_dir)
    if state_path is None:
        raise RuntimeError(
            f"Workspace source node has no succeeded executor state: {node_id}."
        )
    return load_workspace_state(state_path)


def load_workspace_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError(f"Invalid workspace state file: {path.as_posix()}") from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid workspace state payload: {path.as_posix()}")
    return payload


def candidate_source_ref_from_state(path: Path) -> WorktreeSourceRef:
    source_ref = load_source_ref_from_state(path)
    return WorktreeSourceRef(
        source_kind="candidate",
        source_node_id=source_ref.source_node_id,
        source_commit=source_ref.source_commit,
        source_tree=source_ref.source_tree,
        candidate_sequence=source_ref.candidate_sequence,
        bundle_path=source_ref.bundle_path,
        bundle_sha256=source_ref.bundle_sha256,
        bundle_size_bytes=source_ref.bundle_size_bytes,
        bundle_ref=source_ref.bundle_ref,
        upstream_sources=source_ref.upstream_sources,
    )


def _read_preflight_workspace_file(
    plan: PreflightExecutionPlan,
    content_ref: str,
) -> bytes:
    normalized_ref = Path(content_ref)
    if normalized_ref.is_absolute() or ".." in normalized_ref.parts:
        raise ValueError(f"Invalid workspace content reference '{content_ref}'.")
    return (Path(plan.context_root) / "preflight" / normalized_ref).read_bytes()


def _decode_workspace_file(locator_id: str, payload: bytes) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"Workspace file locator content is not valid UTF-8: {locator_id}."
        ) from exc
    if "\x00" in text:
        raise RuntimeError(
            f"Workspace file locator content contains NUL bytes: {locator_id}."
        )
    return text
