from __future__ import annotations

from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.workspace.state import RenderedWorkspaceFileDescriptor

from .models import ResolvedWorkspaceFile


def rendered_workspace_file_descriptor(
    resolved_file: ResolvedWorkspaceFile,
    node_id: str,
    task_id: str,
    role: ProviderRole,
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
    role: ProviderRole,
    round_num: int,
    audit_round_num: int | None,
) -> str:
    audit = f".audit-{audit_round_num}" if audit_round_num is not None else ""
    return f"{node_id}.{role}.{task_id}{audit}.round-{round_num}"
