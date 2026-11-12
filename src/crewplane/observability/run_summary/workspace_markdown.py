"""Workspace plan, invocation evidence, and artifact links for Markdown reports.

Labels are raw text without trailing newlines; renderers escape them once.
Optional facts are omitted when absent, while zero counts and false flags remain.
"""

from __future__ import annotations

from .formatting import format_count, invocation_label
from .markdown_links import artifact_link, markdown_text
from .models import RunSummary, WorkspaceInvocationSummary, WorkspacePlanSummary


def workspace_markdown_lines(summary: RunSummary) -> list[str]:
    """Return escaped plan and invocation bullets, each ending in LF, or a notice."""
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
        lines.append(f"- Plan: {markdown_text('; '.join(compact(details)))}\n")
    if not workspace.invocations:
        lines.append("- No workspace invocations or blob-only inputs recorded.\n")
        return lines
    for invocation in workspace.invocations:
        lines.append(workspace_invocation_line(invocation))
    return lines


def workspace_rendered_file_label(summary: RunSummary) -> str | None:
    """Return raw file counts, or None when the plan or locator count is absent."""
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
    """Return one escaped evidence bullet ending in LF, omitting absent facts."""
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
    return f"- {markdown_text(label)}: {markdown_text('; '.join(compact(details)))}\n"


def workspace_source_label(invocation: WorkspaceInvocationSummary) -> str:
    """Join raw source facts with spaces; return empty text when all are absent."""
    source = invocation.source
    parts = [
        source.kind,
        detail("node", source.node_id),
        detail("commit", source.commit),
        detail("tree", source.tree),
    ]
    return " ".join(compact(parts))


def worktree_contract_label(invocation: WorkspaceInvocationSummary) -> str | None:
    """Return the raw invocation mode[:version], or None when mode is absent."""
    source = invocation.source
    return _worktree_contract_label(
        source.worktree_contract_mode, source.worktree_contract_schema_version
    )


def plan_worktree_contract_label(plan: WorkspacePlanSummary) -> str | None:
    """Return the raw plan mode[:version], or None when mode is absent."""
    return _worktree_contract_label(
        plan.worktree_contract_mode, plan.worktree_contract_schema_version
    )


def _worktree_contract_label(
    mode: str | None, schema_version: str | None
) -> str | None:
    if mode is None:
        return None
    return f"{mode}:{schema_version}" if schema_version is not None else mode


def child_environment_label(invocation: WorkspaceInvocationSummary) -> str | None:
    """Omit unknown requirements; retain an unknown application as literal None."""
    if invocation.child_environment_required is None:
        return None
    return (
        f"required={bool_label(invocation.child_environment_required)} "
        f"applied={bool_label(invocation.child_environment_applied)}"
    )


def workspace_execution_label(invocation: WorkspaceInvocationSummary) -> str | None:
    """Join raw execution facts with commas, or return None when absent."""
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
    """Join raw setup facts with commas, or return None when absent."""
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
    """Join raw reuse facts with commas, preserving false flags; omit absent facts."""
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
    """Join raw drift facts, or return None when the discard decision is unknown."""
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
    """Join raw branch export facts with commas, or return None when absent."""
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


def workspace_result_label(invocation: WorkspaceInvocationSummary) -> str:
    """Join raw result facts with commas; return empty text when all are absent."""
    parts = [
        detail("candidate", invocation.candidate_commit),
        detail("result", invocation.result_commit),
        detail("tree", invocation.result_tree),
        detail("changes", invocation.changed_path_count),
        detail("final_head", invocation.final_head),
    ]
    return ", ".join(compact(parts))


def workspace_bundle_label(invocation: WorkspaceInvocationSummary) -> str:
    """Join raw bundle facts with spaces; return empty text when all are absent."""
    parts = [
        invocation.bundle_path,
        detail("bytes", invocation.bundle_size_bytes),
        detail("verified", bool_label(invocation.bundle_verified)),
    ]
    return " ".join(compact(parts))


def detail(label: str, value: object) -> str | None:
    """Return raw label=value text, omitting only None values."""
    if value is None:
        return None
    return f"{label}={value}"


def bool_label(value: bool | None) -> str | None:
    """Return yes or no for a known flag, or None when unknown."""
    if value is None:
        return None
    return "yes" if value else "no"


def seconds_label(value: float | None) -> str | None:
    """Return seconds to three decimal places, or None when unknown."""
    if value is None:
        return None
    return f"{value:.3f}s"


def joined_optional(*parts: str | None) -> str | None:
    """Join nonempty raw parts with spaces, or return None when none remain."""
    compacted = compact(list(parts))
    return " ".join(compacted) if compacted else None


def comma_optional(parts: list[str | None]) -> str | None:
    """Join nonempty raw parts with commas, or return None when none remain."""
    compacted = compact(parts)
    return ", ".join(compacted) if compacted else None


def compact(parts: list[str | None]) -> list[str]:
    """Keep nonempty parts in their original order without escaping them."""
    return [part for part in parts if part]


def workspace_links(summary: RunSummary) -> str:
    """Return escaped run-relative links with leading and per-bullet LF; empty if disabled."""
    if summary.workspace is None:
        return ""
    lines = []
    for invocation in summary.workspace.invocations:
        paths = (
            ("state", invocation.state_path),
            ("setup log", invocation.setup.log_path),
            ("setup metadata", invocation.setup.metadata_path),
            ("bundle", invocation.bundle_path),
            ("branch export record", invocation.branch_export.record_artifact),
        )
        links = [
            artifact_link(summary, path, label, run_relative=True)
            for label, path in paths
            if path is not None
        ]
        if links:
            identity = invocation_label(
                invocation.node_id,
                invocation.task_id,
                invocation.audit_round_num,
                invocation.round_num,
            )
            lines.append(f"- {markdown_text(identity)} files: {', '.join(links)}\n")
    return "\n" + "".join(lines)
