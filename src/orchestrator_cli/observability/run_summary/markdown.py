from __future__ import annotations

from .formatting import (
    format_cost,
    format_count,
    format_provider_tokens,
    format_visible_estimate,
    invocation_label,
)
from .models import RunSummary, WorkspaceInvocationSummary, WorkspacePlanSummary
from .spend import spend_overview_rows


def render_run_summary_markdown(summary: RunSummary) -> str:
    lines = [
        "# Run Summary\n\n",
        f"- Workflow: {summary.workflow_name}\n",
        f"- Run ID: {summary.run_id}\n",
        f"- Status: {summary.workflow_status}\n",
    ]
    if summary.review_consensus_unresolved:
        lines.append("- Review consensus: unresolved; continued after exhaustion\n")
    lines.extend(
        [
            f"- Started: {summary.started_at}\n",
            f"- Completed: {summary.completed_at}\n",
        ]
    )
    if summary.elapsed_label is not None:
        lines.append(f"- Elapsed: {summary.elapsed_label}\n")
    lines.append("\n## Spend Observability\n\n")
    lines.extend(spend_markdown_lines(summary))
    lines.append("\n## Node Outcomes\n\n")
    lines.extend(node_outcome_markdown_lines(summary))
    lines.append("\n## Workspace Observability\n\n")
    lines.extend(workspace_markdown_lines(summary))
    lines.append("\n## Warnings and Errors\n\n")
    lines.extend(issue_markdown_lines(summary))
    lines.append("\n## Artifact References\n\n")
    lines.extend(artifact_reference_markdown_lines(summary))
    return "".join(lines)


def spend_markdown_lines(summary: RunSummary) -> list[str]:
    if summary.spend is None:
        return ["No spend observability captured.\n"]

    lines = [
        f"- {row.label}: {row.markdown_value()}\n"
        for row in spend_overview_rows(summary.spend)
    ]
    if summary.omitted_invocation_usage_count > 0:
        lines.append(invocation_usage_retention_line(summary))

    for invocation_usage in summary.invocation_usages:
        details = [
            f"captured: {'yes' if invocation_usage.cli_captured else 'no'}",
            f"output extraction: {invocation_usage.output_extraction_status}",
            f"provider report: {invocation_usage.provider_usage_status}",
            f"attempts: {invocation_usage.attempt_count}",
            f"visible estimate: {format_visible_estimate(invocation_usage)}",
            f"configured cost: {format_cost(invocation_usage.configured_cost_usd)}",
            f"confidence: {invocation_usage.invocation_cost_confidence}",
        ]
        details.append(
            "provider tokens: "
            f"{format_provider_tokens(invocation_usage.provider_tokens)}"
        )
        if invocation_usage.usage_parse_error is not None:
            details.append(f"usage parse error: {invocation_usage.usage_parse_error}")
        if invocation_usage.failure_kind is not None:
            details.append(f"failure: {invocation_usage.failure_kind}")
        label = invocation_label(
            node_id=invocation_usage.node_id,
            task_id=invocation_usage.task_id,
            audit_round_num=invocation_usage.audit_round_num,
            round_num=invocation_usage.round_num,
        )
        lines.append(f"- {label}: {'; '.join(details)}\n")
    return lines


def invocation_usage_retention_line(summary: RunSummary) -> str:
    return (
        "- Invocation detail: retained latest "
        f"{len(summary.invocation_usages)} invocation(s); "
        f"{summary.omitted_invocation_usage_count} earlier invocation detail(s) "
        "were omitted from summary detail. Full invocation events remain in "
        f"{summary.event_log_path}.\n"
    )


def node_outcome_markdown_lines(summary: RunSummary) -> list[str]:
    if not summary.node_outcomes:
        return ["No node state captured.\n"]
    lines: list[str] = []
    for node_outcome in summary.node_outcomes:
        result_label = (
            f"; result: {node_outcome.result_path}"
            if node_outcome.result_path is not None
            else "; result: not produced"
        )
        lines.append(
            f"- `{node_outcome.node_id}`: {node_outcome.status}"
            f"{node_outcome.duration_label}{result_label}\n"
        )
    return lines


def workspace_markdown_lines(summary: RunSummary) -> list[str]:
    workspace = summary.workspace
    if workspace is None:
        return [
            "Workspace isolation disabled or no workspace observability captured.\n"
        ]
    lines: list[str] = []
    plan = workspace.plan
    if plan is not None:
        details = [
            detail("contract", plan_worktree_contract_label(plan)),
            detail("source commit", plan.source_commit),
            detail("source tree", plan.source_tree),
            detail("object format", plan.object_format),
            detail("clean_start", plan.clean_start),
            detail(
                "invoker",
                joined_optional(
                    plan.invoker_implementation,
                    detail("launch", plan.invoker_launch_mode),
                    detail(
                        "controlled_env",
                        bool_label(plan.invoker_controlled_child_environment),
                    ),
                ),
            ),
            detail(
                "rendered workspace files",
                workspace_rendered_file_label(summary),
            ),
            detail(
                "cleanup",
                joined_optional(
                    detail("cleanup_on_success", bool_label(plan.cleanup_on_success)),
                    detail(
                        "cache_root_configured",
                        bool_label(plan.cache_root_configured),
                    ),
                ),
            ),
            detail("planned nodes", plan.planned_workspace_node_count),
        ]
        lines.append(f"- Plan: {'; '.join(compact(details))}\n")
    if not workspace.invocations:
        lines.append("- No workspace invocations or blob-only inputs recorded.\n")
        return lines
    for invocation in workspace.invocations:
        lines.append(workspace_invocation_line(invocation))
    return lines


def workspace_rendered_file_label(summary: RunSummary) -> str | None:
    workspace = summary.workspace
    if workspace is None or workspace.plan is None:
        return None
    plan = workspace.plan
    if plan.rendered_locator_count is None:
        return None
    parts = [
        format_count(plan.rendered_locator_count),
        detail("project_initial", plan.rendered_project_initial_count),
        detail("runtime_dynamic", plan.rendered_runtime_dynamic_count),
    ]
    return " ".join(compact(parts))


def workspace_invocation_line(invocation: WorkspaceInvocationSummary) -> str:
    label = invocation_label(
        node_id=invocation.node_id,
        task_id=invocation.task_id,
        audit_round_num=invocation.audit_round_num,
        round_num=invocation.round_num,
    )
    details = [
        detail("kind", invocation.workspace_kind),
        detail("worktree", invocation.logical_worktree_name),
        detail("status", invocation.status),
        detail("source", workspace_source_label(invocation)),
        detail("contract", worktree_contract_label(invocation)),
        detail("checkpoints", invocation.checkpoint_count),
        detail("materialization", invocation.materialization),
        detail("writable", bool_label(invocation.writable)),
        detail("lineage", bool_label(invocation.lineage_producer)),
        detail("env", child_environment_label(invocation)),
        detail("execution", workspace_execution_label(invocation)),
        detail("setup", workspace_setup_label(invocation)),
        detail("reuse", workspace_reuse_label(invocation)),
        detail("reset", invocation.reuse.reset_verification),
        detail("snapshot_drift", workspace_snapshot_drift_label(invocation)),
        detail("rendered_files", invocation.rendered_file_count),
        detail("result", workspace_result_label(invocation)),
        detail("bundle", workspace_bundle_label(invocation)),
        detail("branch_export", workspace_branch_export_label(invocation)),
        detail("diagnostics", invocation.diagnostic_count),
        detail("retention", invocation.retention),
        detail("state", invocation.state_path),
    ]
    return f"- {label}: {'; '.join(compact(details))}\n"


def workspace_source_label(invocation: WorkspaceInvocationSummary) -> str | None:
    source = invocation.source
    parts = [
        source.kind,
        detail("node", source.node_id),
        detail("commit", source.commit),
        detail("tree", source.tree),
    ]
    return " ".join(compact(parts))


def worktree_contract_label(invocation: WorkspaceInvocationSummary) -> str | None:
    source = invocation.source
    if source.worktree_contract_mode is None:
        return None
    if source.worktree_contract_schema_version is None:
        return source.worktree_contract_mode
    return f"{source.worktree_contract_mode}:{source.worktree_contract_schema_version}"


def plan_worktree_contract_label(plan: WorkspacePlanSummary) -> str | None:
    mode = plan.worktree_contract_mode
    schema_version = plan.worktree_contract_schema_version
    if mode is None:
        return None
    return f"{mode}:{schema_version}" if schema_version is not None else mode


def child_environment_label(invocation: WorkspaceInvocationSummary) -> str | None:
    if invocation.child_environment_required is None:
        return None
    return (
        f"required={bool_label(invocation.child_environment_required)} "
        f"applied={bool_label(invocation.child_environment_applied)}"
    )


def workspace_execution_label(invocation: WorkspaceInvocationSummary) -> str | None:
    execution = invocation.execution
    parts = [
        detail("cache_root", execution.cache_root),
        detail("workspace", execution.workspace_path),
        detail("checkout", execution.checkout_root),
        detail("cwd", execution.effective_cwd),
        detail("checkout_bytes", execution.checkout_size_bytes),
        detail("provisioning", seconds_label(execution.provisioning_duration_seconds)),
    ]
    return comma_optional(parts)


def workspace_setup_label(invocation: WorkspaceInvocationSummary) -> str | None:
    setup = invocation.setup
    parts = [
        setup.profile_name,
        detail("status", setup.status),
        detail("duration", seconds_label(setup.duration_seconds)),
        detail("commands", setup.command_count),
        detail("log", setup.log_path),
        detail("metadata", setup.metadata_path),
        detail("failure", setup.failure_message),
    ]
    return comma_optional(parts)


def workspace_reuse_label(invocation: WorkspaceInvocationSummary) -> str | None:
    reuse = invocation.reuse
    parts = [
        reuse.strategy,
        detail("reused", bool_label(reuse.reused)),
        detail("fallback", bool_label(reuse.fallback)),
        detail("previous_state", reuse.previous_workspace_state),
        detail("reason", reuse.fallback_reason),
    ]
    return comma_optional(parts)


def workspace_snapshot_drift_label(
    invocation: WorkspaceInvocationSummary,
) -> str | None:
    if invocation.snapshot_drift_discarded is None:
        return None
    parts = [
        detail("discarded", bool_label(invocation.snapshot_drift_discarded)),
        detail("changes", invocation.changed_path_count),
        detail("reported_paths", invocation.snapshot_changed_paths_reported),
        detail("truncated", bool_label(invocation.snapshot_changed_paths_truncated)),
    ]
    return comma_optional(parts)


def workspace_branch_export_label(
    invocation: WorkspaceInvocationSummary,
) -> str | None:
    branch_export = invocation.branch_export
    parts = [
        detail("status", branch_export.status),
        detail("operation", branch_export.operation),
        detail("branch", branch_export.branch_name),
        detail("ref", branch_export.branch_ref),
        detail("record", branch_export.record_artifact),
        detail("failure", branch_export.failure_message),
    ]
    return comma_optional(parts)


def workspace_result_label(invocation: WorkspaceInvocationSummary) -> str | None:
    parts = [
        detail("candidate", invocation.candidate_commit),
        detail("result", invocation.result_commit),
        detail("tree", invocation.result_tree),
        detail("changes", invocation.changed_path_count),
        detail("final_head", invocation.final_head),
    ]
    return ", ".join(compact(parts))


def workspace_bundle_label(invocation: WorkspaceInvocationSummary) -> str | None:
    parts = [
        invocation.bundle_path,
        detail("bytes", invocation.bundle_size_bytes),
        detail("verified", bool_label(invocation.bundle_verified)),
    ]
    return " ".join(compact(parts))


def detail(label: str, value: object) -> str | None:
    if value is None:
        return None
    return f"{label}={value}"


def bool_label(value: bool | None) -> str | None:
    if value is None:
        return None
    return "yes" if value else "no"


def seconds_label(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:.3f}s"


def joined_optional(*parts: str | None) -> str | None:
    compacted = compact(list(parts))
    return " ".join(compacted) if compacted else None


def comma_optional(parts: list[str | None]) -> str | None:
    compacted = compact(parts)
    return ", ".join(compacted) if compacted else None


def compact(parts: list[str | None]) -> list[str]:
    return [part for part in parts if part]


def issue_markdown_lines(summary: RunSummary) -> list[str]:
    if not summary.issues:
        return ["No warnings or errors recorded.\n"]
    return [f"- {issue.message}\n" for issue in summary.issues]


def artifact_reference_markdown_lines(summary: RunSummary) -> list[str]:
    if not summary.artifact_references:
        return ["No invocation artifacts captured.\n"]
    lines: list[str] = []
    for reference in summary.artifact_references:
        parts = [
            invocation_label(
                node_id=reference.node_id,
                task_id=reference.task_id,
                audit_round_num=reference.audit_round_num,
                round_num=reference.round_num,
            )
        ]
        if reference.output_file is not None:
            parts.append(f"output: {reference.output_file}")
        if reference.log_file is not None:
            parts.append(f"log: {reference.log_file}")
        lines.append(f"- {'; '.join(parts)}\n")
    return lines
