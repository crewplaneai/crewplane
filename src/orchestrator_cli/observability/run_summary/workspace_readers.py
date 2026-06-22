from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from orchestrator_cli.version import SCHEMA_VERSION

from .models import (
    WorkspaceInvocationBranchExportSummary,
    WorkspaceInvocationExecutionSummary,
    WorkspaceInvocationReuseSummary,
    WorkspaceInvocationSetupSummary,
    WorkspaceInvocationSourceSummary,
    WorkspaceInvocationSummary,
    WorkspacePlanSummary,
)
from .workspace_state_paths import (
    workspace_state_candidate_paths,
    workspace_state_path_matches_node,
)
from .workspace_values import (
    artifact_relative_path,
    bool_or_none,
    bundle_path_label,
    first_available_section_value,
    float_or_none,
    int_or_none,
    list_count_or_none,
    mapping_or_none,
    read_json_mapping,
    reset_verification_status,
    section_value,
    state_relative_artifact_path,
    string_or_none,
)


def workspace_plan_summary(stages_dir: Path) -> WorkspacePlanSummary | None:
    descriptor = workspace_descriptor(stages_dir)
    if descriptor is None:
        return None
    source_section = mapping_or_none(descriptor.get("source"))
    invoker_section = mapping_or_none(descriptor.get("invoker"))
    rendered_files_section = mapping_or_none(descriptor.get("rendered_files"))
    cleanup_section = mapping_or_none(descriptor.get("cleanup"))
    worktree_contract_section = mapping_or_none(descriptor.get("worktree_contract"))
    node_descriptors = descriptor.get("nodes")
    return WorkspacePlanSummary(
        worktree_contract_mode=section_value(
            worktree_contract_section,
            "mode",
            string_or_none,
        ),
        worktree_contract_schema_version=section_value(
            worktree_contract_section,
            "schema_version",
            string_or_none,
        ),
        source_commit=section_value(
            source_section,
            "run_base_commit",
            string_or_none,
        ),
        source_tree=section_value(source_section, "source_tree", string_or_none),
        object_format=section_value(source_section, "object_format", string_or_none),
        clean_start=section_value(source_section, "clean_start", string_or_none),
        invoker_implementation=section_value(
            invoker_section,
            "implementation",
            string_or_none,
        ),
        invoker_launch_mode=section_value(
            invoker_section,
            "launch_mode",
            string_or_none,
        ),
        invoker_controlled_child_environment=section_value(
            invoker_section,
            "controlled_child_environment",
            bool_or_none,
        ),
        rendered_locator_count=section_value(
            rendered_files_section,
            "locator_count",
            int_or_none,
        ),
        rendered_project_initial_count=section_value(
            rendered_files_section,
            "project_initial",
            int_or_none,
        ),
        rendered_runtime_dynamic_count=section_value(
            rendered_files_section,
            "runtime_dynamic",
            int_or_none,
        ),
        cleanup_on_success=section_value(
            cleanup_section,
            "cleanup_on_success",
            bool_or_none,
        ),
        cache_root_configured=section_value(
            cleanup_section,
            "cache_root_configured",
            bool_or_none,
        ),
        planned_workspace_node_count=len(node_descriptors)
        if isinstance(node_descriptors, list)
        else 0,
    )


def workspace_descriptor(stages_dir: Path) -> Mapping[str, object] | None:
    for path in (
        stages_dir / "manifests" / "run.json",
        stages_dir / "preflight" / "manifest.json",
    ):
        payload = read_json_mapping(path)
        workspace = payload.get("workspace") if payload is not None else None
        if isinstance(workspace, Mapping):
            return workspace
    return None


def workspace_state_summaries(
    stages_dir: Path,
) -> tuple[WorkspaceInvocationSummary, ...]:
    summaries: list[WorkspaceInvocationSummary] = []
    for path in workspace_state_candidate_paths(stages_dir):
        payload = read_json_mapping(path)
        if payload is None or payload.get("version") != SCHEMA_VERSION:
            continue
        if not workspace_state_path_matches_node(path, payload):
            continue
        summary = workspace_state_summary(stages_dir, path, payload)
        if summary is not None:
            summaries.append(summary)
    return tuple(summaries)


def workspace_state_summary(
    stages_dir: Path,
    state_path: Path,
    payload: Mapping[str, object],
) -> WorkspaceInvocationSummary | None:
    source_section = mapping_or_none(
        payload.get("invocation_source")
    ) or mapping_or_none(payload.get("source"))
    workspace_section = mapping_or_none(payload.get("workspace"))
    result_section = mapping_or_none(payload.get("result"))
    bundle_section = mapping_or_none(payload.get("bundle"))
    child_environment_section = mapping_or_none(
        payload.get("child_process_environment")
    )
    worktree_contract_section = mapping_or_none(payload.get("worktree_contract"))
    execution_section = mapping_or_none(payload.get("execution"))
    setup_section = mapping_or_none(payload.get("setup"))
    reuse_section = mapping_or_none(payload.get("reuse"))
    branch_export_section = mapping_or_none(payload.get("branch_export"))
    diagnostics = payload.get("diagnostics")
    rendered_files = payload.get("rendered_workspace_files")
    return WorkspaceInvocationSummary(
        node_id=string_or_none(payload.get("node_id")),
        task_id=string_or_none(payload.get("task_id")),
        audit_round_num=int_or_none(payload.get("audit_round_num")),
        round_num=int_or_none(payload.get("round_num")),
        workspace_kind=string_or_none(payload.get("workspace_kind")),
        logical_worktree_name=string_or_none(payload.get("logical_worktree_name")),
        status=string_or_none(payload.get("status")),
        state_path=artifact_relative_path(stages_dir, state_path),
        writable=section_value(workspace_section, "writable", bool_or_none),
        lineage_producer=section_value(
            workspace_section,
            "lineage_producer",
            bool_or_none,
        ),
        child_environment_required=section_value(
            child_environment_section,
            "required",
            bool_or_none,
        ),
        child_environment_applied=section_value(
            child_environment_section,
            "applied",
            bool_or_none,
        ),
        source=workspace_source_summary(source_section, worktree_contract_section),
        materialization=section_value(
            workspace_section,
            "materialization",
            string_or_none,
        ),
        retention=section_value(workspace_section, "retention", string_or_none),
        result_commit=section_value(result_section, "result_commit", string_or_none),
        result_tree=section_value(result_section, "result_tree", string_or_none),
        candidate_commit=section_value(
            result_section,
            "candidate_commit",
            string_or_none,
        ),
        candidate_tree=section_value(
            result_section,
            "candidate_tree",
            string_or_none,
        ),
        final_head=section_value(result_section, "final_head", string_or_none),
        changed_path_count=section_value(
            result_section,
            "changed_path_count",
            int_or_none,
        ),
        bundle_path=section_value(bundle_section, "path", bundle_path_label),
        bundle_size_bytes=section_value(bundle_section, "size_bytes", int_or_none),
        bundle_verified=section_value(bundle_section, "verified", bool_or_none),
        rendered_file_count=list_count_or_none(rendered_files),
        diagnostic_count=list_count_or_none(diagnostics),
        execution=workspace_execution_summary(execution_section),
        setup=workspace_setup_summary(stages_dir, state_path, setup_section),
        reuse=workspace_reuse_summary(reuse_section),
        snapshot_drift_discarded=section_value(
            result_section,
            "snapshot_drift_discarded",
            bool_or_none,
        ),
        snapshot_changed_paths_reported=section_value(
            result_section,
            "changed_paths",
            list_count_or_none,
        ),
        snapshot_changed_paths_truncated=section_value(
            result_section,
            "changed_paths_truncated",
            bool_or_none,
        ),
        branch_export=workspace_branch_export_summary(branch_export_section),
    )


def workspace_source_summary(
    source_section: Mapping[str, object] | None,
    worktree_contract_section: Mapping[str, object] | None,
) -> WorkspaceInvocationSourceSummary:
    return WorkspaceInvocationSourceSummary(
        kind=first_available_section_value(
            source_section,
            ("source_kind", "kind"),
            string_or_none,
        ),
        node_id=first_available_section_value(
            source_section,
            ("source_node_id", "node_id"),
            string_or_none,
        ),
        commit=first_available_section_value(
            source_section,
            ("source_commit", "commit"),
            string_or_none,
        ),
        tree=first_available_section_value(
            source_section,
            ("source_tree", "tree"),
            string_or_none,
        ),
        worktree_contract_mode=section_value(
            worktree_contract_section,
            "mode",
            string_or_none,
        ),
        worktree_contract_schema_version=section_value(
            worktree_contract_section,
            "schema_version",
            string_or_none,
        ),
    )


def workspace_execution_summary(
    execution_section: Mapping[str, object] | None,
) -> WorkspaceInvocationExecutionSummary:
    return WorkspaceInvocationExecutionSummary(
        cache_root=section_value(execution_section, "cache_root", string_or_none),
        workspace_path=section_value(
            execution_section,
            "workspace_path",
            string_or_none,
        ),
        checkout_root=section_value(
            execution_section,
            "checkout_root",
            string_or_none,
        ),
        effective_cwd=section_value(
            execution_section,
            "effective_cwd",
            string_or_none,
        ),
        checkout_size_bytes=section_value(
            execution_section,
            "checkout_size_bytes",
            int_or_none,
        ),
        provisioning_duration_seconds=section_value(
            execution_section,
            "provisioning_duration_seconds",
            float_or_none,
        ),
    )


def workspace_setup_summary(
    stages_dir: Path,
    state_path: Path,
    setup_section: Mapping[str, object] | None,
) -> WorkspaceInvocationSetupSummary:
    return WorkspaceInvocationSetupSummary(
        profile_name=section_value(setup_section, "profile_name", string_or_none),
        status=section_value(setup_section, "status", string_or_none),
        duration_seconds=section_value(
            setup_section,
            "duration_seconds",
            float_or_none,
        ),
        failure_message=section_value(
            setup_section,
            "failure_message",
            string_or_none,
        ),
        command_count=section_value(setup_section, "commands", list_count_or_none),
        log_path=_setup_artifact_path(
            stages_dir, state_path, setup_section, "log_path"
        ),
        metadata_path=_setup_artifact_path(
            stages_dir,
            state_path,
            setup_section,
            "metadata_path",
        ),
    )


def _setup_artifact_path(
    stages_dir: Path,
    state_path: Path,
    setup_section: Mapping[str, object] | None,
    field_name: str,
) -> str | None:
    if setup_section is None:
        return None
    return state_relative_artifact_path(
        stages_dir,
        state_path,
        setup_section.get(field_name),
    )


def workspace_reuse_summary(
    reuse_section: Mapping[str, object] | None,
) -> WorkspaceInvocationReuseSummary:
    return WorkspaceInvocationReuseSummary(
        strategy=section_value(reuse_section, "strategy", string_or_none),
        reused=section_value(reuse_section, "reused", bool_or_none),
        fallback=section_value(reuse_section, "fallback", bool_or_none),
        fallback_reason=section_value(
            reuse_section,
            "fallback_reason",
            string_or_none,
        ),
        previous_workspace_state=section_value(
            reuse_section,
            "previous_workspace_state",
            string_or_none,
        ),
        reset_verification=reset_verification_status(reuse_section),
    )


def workspace_branch_export_summary(
    branch_export_section: Mapping[str, object] | None,
) -> WorkspaceInvocationBranchExportSummary:
    return WorkspaceInvocationBranchExportSummary(
        status=section_value(branch_export_section, "status", string_or_none),
        operation=section_value(branch_export_section, "operation", string_or_none),
        branch_name=section_value(branch_export_section, "branch_name", string_or_none),
        branch_ref=section_value(branch_export_section, "branch_ref", string_or_none),
        record_artifact=section_value(
            branch_export_section,
            "record_artifact",
            string_or_none,
        ),
        failure_message=section_value(
            branch_export_section,
            "failure_message",
            string_or_none,
        ),
    )
