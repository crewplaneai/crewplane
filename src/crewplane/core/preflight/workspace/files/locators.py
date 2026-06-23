from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path, PurePosixPath

from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan

from ...compile_state import (
    CompileState,
    PreflightCompileOptions,
    append_diagnostic,
    source_file,
    source_root,
    token_source_span,
)
from ...diagnostics import PreflightDiagnosticCode, PreflightDiagnosticPhase
from ...models import (
    WorkspaceFileLocator,
    WorkspaceFileSourceClass,
    WorkspaceFileTarget,
)
from ...references import TemplateReference
from ...signatures import signature_for_payload
from .git_reads import (
    SUPPORTED_FILE_MODES,
    ProjectBlobRecord,
    git_cat_blob,
    git_error,
    git_ls_tree,
    valid_utf8_without_nul,
)
from .paths import (
    WorkspaceFilePathRecord,
    is_reserved_workspace_path,
    lexical_absolute_path,
    project_relative_workspace_path,
    source_root_relative_to_project,
)
from .selection import (
    has_same_worktree_source_ancestor,
    selected_worktree_kind,
    selected_worktree_name,
)


def resolve_workspace_file_reference(
    workflow: WorkflowPlan,
    node: WorkflowNode,
    target: WorkspaceFileTarget,
    segment_index: int | None,
    reference: TemplateReference,
    occurrence_id: str,
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    source_snapshot = options.workspace_source_snapshot
    if source_snapshot is None:
        raise ValueError("Workspace file locators require a workspace source snapshot.")
    raw_path = reference.key or ""
    path_result = workspace_file_path_record(node, raw_path, options, state)
    if path_result is None:
        return
    source_class = locator_source_class(workflow, node, target)
    blob_record = None
    if source_class == "project_initial":
        blob_record = resolve_project_blob(
            source_snapshot.git_top_level,
            source_snapshot.run_base_commit,
            path_result.git_top_relative_path,
            node.id,
            raw_path,
            state,
        )
        if blob_record is None:
            return
    locator = build_workspace_file_locator(
        node=node,
        target=target,
        segment_index=segment_index,
        reference=reference,
        occurrence_id=occurrence_id,
        options=options,
        workflow=workflow,
        source_class=source_class,
        path_record=path_result,
        blob_record=blob_record,
    )
    state.workspace_file_references[occurrence_id] = locator
    state.workspace_file_locators.append(locator)
    if blob_record is not None and locator.content_ref is not None:
        state.workspace_file_payloads.setdefault(
            locator.content_ref, blob_record.payload
        )


def workspace_file_path_record(
    node: WorkflowNode,
    raw_path: str,
    options: PreflightCompileOptions,
    state: CompileState,
) -> WorkspaceFilePathRecord | None:
    raw = raw_path.strip()
    if not raw:
        append_workspace_file_error(
            state,
            node.id,
            raw_path,
            "Workspace file path is empty.",
        )
        return None
    if "\x00" in raw:  # checks whether the {{file:...}} path contains a NUL byte
        append_workspace_file_error(
            state,
            node.id,
            raw_path,
            "Workspace file paths must not contain NUL bytes.",
        )
        return None
    absolute_path = is_absolute_workspace_file_path(raw, node.id, raw_path, state)
    if absolute_path is None:
        return None
    if absolute_path:
        append_workspace_file_error(
            state,
            node.id,
            raw_path,
            "Workspace-enabled absolute file tokens must be explicitly allowlisted.",
        )
        return None
    project_root = lexical_absolute_path(options.project_root)
    node_source_root = lexical_absolute_path(source_root(node, options))
    source_relative = source_root_relative_to_project(node_source_root, project_root)
    if source_relative is None:
        append_workspace_file_error(
            state,
            node.id,
            raw_path,
            "Workspace file token source root escapes the project root.",
        )
        return None
    project_relative = project_relative_workspace_path(source_relative, raw)
    if project_relative is None:
        append_workspace_file_error(
            state,
            node.id,
            raw_path,
            "Workspace file token escapes the project root.",
        )
        return None
    if is_reserved_workspace_path(project_relative):
        append_workspace_file_error(
            state,
            node.id,
            raw_path,
            "Workspace file tokens cannot read reserved .crewplane runtime roots.",
        )
        return None
    git_path = git_top_relative_path(
        options.workspace_source_snapshot.project_root_relative_path
        if options.workspace_source_snapshot is not None
        else ".",
        project_relative,
    )
    return WorkspaceFilePathRecord(
        source_root=node_source_root.as_posix(),
        source_root_relative_to_project=source_relative,
        git_top_relative_path=git_path,
        workspace_relative_path=project_relative,
    )


def is_absolute_workspace_file_path(
    raw: str,
    node_id: str,
    raw_path: str,
    state: CompileState,
) -> bool | None:
    try:
        return Path(raw).expanduser().is_absolute()
    except RuntimeError as exc:
        append_workspace_file_error(
            state,
            node_id,
            raw_path,
            f"Workspace file path could not expand user home: {exc}",
        )
        return None


def locator_source_class(
    workflow: WorkflowPlan,
    node: WorkflowNode,
    target: WorkspaceFileTarget,
) -> WorkspaceFileSourceClass:
    if target == "input_output":
        return "project_initial"
    selector = selected_worktree_name(workflow, node)
    if selector is None:
        return "project_initial"
    declaration = workflow.worktrees[selector]
    if declaration.kind == "snapshot":
        return "project_initial"
    if target == "reviewer_prompt":
        return "runtime_dynamic"
    if has_same_worktree_source_ancestor(workflow, node, selector):
        return "runtime_dynamic"
    return "project_initial"


def build_workspace_file_locator(
    node: WorkflowNode,
    target: WorkspaceFileTarget,
    segment_index: int | None,
    reference: TemplateReference,
    occurrence_id: str,
    options: PreflightCompileOptions,
    workflow: WorkflowPlan,
    source_class: WorkspaceFileSourceClass,
    path_record: WorkspaceFilePathRecord,
    blob_record: ProjectBlobRecord | None,
) -> WorkspaceFileLocator:
    digest = hashlib.sha256(blob_record.payload).hexdigest() if blob_record else None
    locator_payload = {
        "git_blob": blob_record.object_id if blob_record else None,
        "git_file_mode": blob_record.mode if blob_record else None,
        "git_top_relative_path": path_record.git_top_relative_path,
        "node_id": node.id,
        "occurrence_id": occurrence_id,
        "raw_path": reference.key or "",
        "source_class": source_class,
        "target": target,
        "worktree_contract": (
            options.workspace_source_snapshot.worktree_contract.model_dump(mode="json")
            if options.workspace_source_snapshot is not None
            else None
        ),
    }
    locator_id = f"workspace-file-{signature_for_payload(locator_payload)}"
    runtime_dynamic_after_candidate = (
        target == "executor_prompt"
        and selected_worktree_kind(workflow, node) == "worktree"
    )
    content_ref = f"workspace-files/{locator_id}.txt" if blob_record else None
    return WorkspaceFileLocator(
        locator_id=locator_id,
        content_ref=content_ref,
        occurrence_id=occurrence_id,
        node_id=node.id,
        target=target,
        source_class=source_class,
        raw_token=reference.raw_token,
        raw_path=reference.key or "",
        source_file=source_file(node, options),
        source_span=(
            token_source_span(node, options, segment_index, reference)
            if segment_index is not None
            else None
        ),
        token_raw_span={"start": reference.start, "end": reference.end},
        source_root=path_record.source_root,
        source_root_relative_to_project=path_record.source_root_relative_to_project,
        project_root_relative_to_git_top=(
            options.workspace_source_snapshot.project_root_relative_path
            if options.workspace_source_snapshot is not None
            else "."
        ),
        git_top_relative_path=path_record.git_top_relative_path,
        workspace_relative_path=path_record.workspace_relative_path,
        runtime_dynamic_after_candidate=runtime_dynamic_after_candidate,
        git_blob=blob_record.object_id if blob_record else None,
        git_file_mode=blob_record.mode if blob_record else None,
        byte_size=len(blob_record.payload) if blob_record else None,
        canonical_blob_sha256=digest,
        injected_sha256=None,
        literal_path_verified=blob_record is not None,
        utf8_validated=blob_record is not None,
    )


def token_signature_for_workspace_locator(locator: WorkspaceFileLocator) -> str:
    return signature_for_payload(locator.model_dump(mode="json"))


def workspace_locator_resolved_payload(
    locator: WorkspaceFileLocator,
) -> dict[str, str]:
    payload = {
        "kind": "workspace_file_locator",
        "locator_id": locator.locator_id,
        "source_class": locator.source_class,
        "target": locator.target,
        "workspace_relative_path": locator.workspace_relative_path,
        "git_top_relative_path": locator.git_top_relative_path,
    }
    if locator.git_blob is not None:
        payload["git_blob"] = locator.git_blob
    if locator.canonical_blob_sha256 is not None:
        payload["canonical_blob_sha256"] = locator.canonical_blob_sha256
    if locator.content_ref is not None:
        payload["content_ref"] = locator.content_ref
    return payload


def workspace_locator_metadata(locator: WorkspaceFileLocator) -> dict[str, str]:
    metadata = {
        "locator_id": locator.locator_id,
        "source_class": locator.source_class,
        "workspace_relative_path": locator.workspace_relative_path,
    }
    if locator.git_blob is not None:
        metadata["git_blob"] = locator.git_blob
    if locator.canonical_blob_sha256 is not None:
        metadata["canonical_blob_sha256"] = locator.canonical_blob_sha256
    if locator.content_ref is not None:
        metadata["content_ref"] = locator.content_ref
    return metadata


def resolve_project_blob(
    git_top_level: str,
    run_base_commit: str,
    git_top_relative_path: str,
    node_id: str,
    raw_path: str,
    state: CompileState,
) -> ProjectBlobRecord | None:
    try:
        record = git_ls_tree(git_top_level, run_base_commit, git_top_relative_path)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        append_workspace_file_error(
            state,
            node_id,
            raw_path,
            f"Workspace file locator Git lookup failed: {git_error(exc)}",
        )
        return None
    if record is None:
        append_workspace_file_error(
            state,
            node_id,
            raw_path,
            "Workspace file token does not resolve at the run base commit.",
        )
        return None
    if record.path != git_top_relative_path:
        append_workspace_file_error(
            state,
            node_id,
            raw_path,
            "Workspace file token resolved to an unexpected Git path.",
        )
        return None
    if record.object_type != "blob" or record.mode not in SUPPORTED_FILE_MODES:
        append_workspace_file_error(
            state,
            node_id,
            raw_path,
            "Workspace file tokens must resolve to regular Git blob files.",
        )
        return None
    try:
        payload = git_cat_blob(git_top_level, record.object_id)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        append_workspace_file_error(
            state,
            node_id,
            raw_path,
            f"Workspace file locator blob read failed: {git_error(exc)}",
        )
        return None
    if not valid_utf8_without_nul(payload):
        append_workspace_file_error(
            state,
            node_id,
            raw_path,
            "Workspace file token content must be UTF-8 text without NUL bytes.",
        )
        return None
    return ProjectBlobRecord(
        mode=record.mode,
        object_type=record.object_type,
        object_id=record.object_id,
        path=record.path,
        payload=payload,
    )


def git_top_relative_path(project_root_relative: str, project_relative: str) -> str:
    if project_root_relative == ".":
        return project_relative
    return PurePosixPath(project_root_relative, project_relative).as_posix()


def append_workspace_file_error(
    state: CompileState,
    node_id: str,
    raw_path: str,
    message: str,
) -> None:
    append_diagnostic(
        state,
        code=PreflightDiagnosticCode.WORKSPACE_FILE_LOCATOR,
        phase=PreflightDiagnosticPhase.WORKSPACE_FILE_LOCATOR_POLICY,
        node_id=node_id,
        path=raw_path,
        message=message,
    )
