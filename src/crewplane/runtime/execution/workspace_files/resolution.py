from __future__ import annotations

import hashlib
from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceFileLocator,
    WorkspaceFileSourceClass,
)
from crewplane.core.preflight.workspace.files.git_reads import (
    GitTreeRecord,
    git_cat_blob,
    git_ls_tree,
    valid_utf8_without_nul,
)
from crewplane.core.workspace.git_policy import REGULAR_FILE_MODES
from crewplane.runtime.workspace.plan_nodes import workspace_plan_node
from crewplane.runtime.workspace.worktree import (
    WorktreeSourceRef,
    ensure_source_commit_available,
)
from crewplane.runtime.workspace.worktree.temporary_refs import TemporaryRefOwner

from ..errors import NodeExecutionError
from .models import ResolvedWorkspaceFile
from .source_resolution import (
    WorkspaceCandidateSourceContext,
    project_source_ref,
    uses_candidate_source,
)
from .source_selection import dynamic_locator_source

_PROJECT_INITIAL_SOURCE_CLASSES = {
    WorkspaceFileSourceClass.PROJECT_INITIAL,
    WorkspaceFileSourceClass.PROJECT_INITIAL_THEN_CANDIDATE,
}


def resolve_project_initial_workspace_file(
    plan: PreflightExecutionPlan,
    locator_id: str,
) -> ResolvedWorkspaceFile:
    locator = workspace_file_locator(plan, locator_id)
    content_ref = _project_initial_content_ref(locator, locator_id)
    payload = _read_preflight_workspace_file(plan, content_ref)
    digest = hashlib.sha256(payload).hexdigest()
    _validate_preflight_content_identity(locator, locator_id, payload, digest)
    text = _decode_workspace_file(locator_id, payload)
    return _project_initial_result(plan, locator, text, payload, digest)


def resolve_workspace_file(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    locator_id: str,
    workspace_candidate_source: bool = False,
    workspace_candidate_context: WorkspaceCandidateSourceContext | None = None,
) -> ResolvedWorkspaceFile:
    locator = workspace_file_locator(plan, locator_id)
    if _uses_project_initial_content(
        locator,
        workspace_candidate_source,
        workspace_candidate_context,
    ):
        return resolve_project_initial_workspace_file(plan, locator_id)
    return _resolve_dynamic_workspace_file(
        plan,
        output,
        locator,
        workspace_candidate_source,
        workspace_candidate_context,
    )


def workspace_file_locator(
    plan: PreflightExecutionPlan,
    locator_id: str,
) -> WorkspaceFileLocator:
    for locator in plan.workspace_file_locators:
        if locator.locator_id == locator_id:
            return locator
    raise RuntimeError(f"Workspace file locator not found in plan: {locator_id}.")


def read_dynamic_locator_blob(
    plan: PreflightExecutionPlan,
    locator: WorkspaceFileLocator,
    source: WorktreeSourceRef,
    owner: TemporaryRefOwner,
) -> tuple[bytes, str, str]:
    workspace_source = plan.workspace_source
    if workspace_source is None:
        raise RuntimeError(
            f"Workspace file locator has no source snapshot: {locator.locator_id}."
        )
    with ensure_source_commit_available(workspace_source, source, owner):
        record = git_ls_tree(
            workspace_source.git_top_level,
            source.source_commit,
            locator.git_top_relative_path,
        )
        verified_record = _require_exact_regular_blob(locator, record)
        payload = git_cat_blob(
            workspace_source.git_top_level,
            verified_record.object_id,
        )
    _require_utf8_text_blob(locator, payload)
    return payload, verified_record.object_id, verified_record.mode


def _project_initial_content_ref(
    locator: WorkspaceFileLocator,
    locator_id: str,
) -> str:
    if locator.source_class not in _PROJECT_INITIAL_SOURCE_CLASSES:
        raise RuntimeError(
            "Runtime-dynamic workspace file locator resolution is unavailable in "
            f"this build: {locator_id}."
        )
    if locator.content_ref is None:
        raise RuntimeError(
            f"Workspace file locator is missing preflight content: {locator_id}."
        )
    return locator.content_ref


def _validate_preflight_content_identity(
    locator: WorkspaceFileLocator,
    locator_id: str,
    payload: bytes,
    digest: str,
) -> None:
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


def _project_initial_result(
    plan: PreflightExecutionPlan,
    locator: WorkspaceFileLocator,
    text: str,
    payload: bytes,
    digest: str,
) -> ResolvedWorkspaceFile:
    return ResolvedWorkspaceFile(
        locator=locator,
        text=text,
        byte_size=len(payload),
        sha256=digest,
        source_ref=project_source_ref(plan),
        git_blob=locator.git_blob,
        git_file_mode=locator.git_file_mode,
        literal_path_verified=locator.literal_path_verified,
        utf8_validated=locator.utf8_validated,
    )


def _uses_project_initial_content(
    locator: WorkspaceFileLocator,
    workspace_candidate_source: bool,
    context: WorkspaceCandidateSourceContext | None,
) -> bool:
    return locator.source_class in _PROJECT_INITIAL_SOURCE_CLASSES and not (
        uses_candidate_source(locator, workspace_candidate_source, context)
    )


def _resolve_dynamic_workspace_file(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    locator: WorkspaceFileLocator,
    workspace_candidate_source: bool,
    context: WorkspaceCandidateSourceContext | None,
) -> ResolvedWorkspaceFile:
    source = dynamic_locator_source(
        plan,
        output,
        locator,
        workspace_candidate_source,
        context,
    )
    workspace_source = plan.workspace_source
    if workspace_source is None:
        raise RuntimeError(
            f"Workspace file locator has no source snapshot: {locator.locator_id}."
        )
    consumer_node = workspace_plan_node(plan, locator.node_id)
    owner = TemporaryRefOwner.dedicated(
        plan,
        workspace_source,
        output.get_run_log_dir(),
        consumer_node.id,
        f"workspace-file-{locator.locator_id}",
    )
    payload, git_blob, git_file_mode = read_dynamic_locator_blob(
        plan,
        locator,
        source,
        owner,
    )
    return _dynamic_result(locator, source, payload, git_blob, git_file_mode)


def _dynamic_result(
    locator: WorkspaceFileLocator,
    source: WorktreeSourceRef,
    payload: bytes,
    git_blob: str,
    git_file_mode: str,
) -> ResolvedWorkspaceFile:
    digest = hashlib.sha256(payload).hexdigest()
    return ResolvedWorkspaceFile(
        locator=locator,
        text=_decode_workspace_file(locator.locator_id, payload),
        byte_size=len(payload),
        sha256=digest,
        source_ref=source,
        git_blob=git_blob,
        git_file_mode=git_file_mode,
        literal_path_verified=True,
        utf8_validated=True,
    )


def _require_exact_regular_blob(
    locator: WorkspaceFileLocator,
    record: GitTreeRecord | None,
) -> GitTreeRecord:
    if record is None or record.path != locator.git_top_relative_path:
        raise NodeExecutionError(
            "Runtime-dynamic workspace file locator does not resolve exactly: "
            f"{locator.locator_id}."
        )
    if record.object_type != "blob" or record.mode not in REGULAR_FILE_MODES:
        raise NodeExecutionError(
            "Runtime-dynamic workspace file locator must resolve to a regular "
            f"Git blob: {locator.locator_id}."
        )
    return record


def _require_utf8_text_blob(locator: WorkspaceFileLocator, payload: bytes) -> None:
    if not valid_utf8_without_nul(payload):
        raise NodeExecutionError(
            "Runtime-dynamic workspace file locator content must be UTF-8 text "
            f"without NUL bytes: {locator.locator_id}."
        )


def _read_preflight_workspace_file(
    plan: PreflightExecutionPlan,
    content_ref: str,
) -> bytes:
    normalized_ref = Path(content_ref)
    if normalized_ref.is_absolute() or ".." in normalized_ref.parts:
        raise ValueError(f"Invalid workspace content reference '{content_ref}'.")
    path = contained_regular_file(
        Path(plan.context_root) / "preflight",
        normalized_ref.as_posix(),
    )
    if path is None:
        raise RuntimeError(
            f"Workspace content reference is missing or unsafe: '{content_ref}'."
        )
    return path.read_bytes()


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
