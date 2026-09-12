from __future__ import annotations

from collections.abc import Sequence

from .formatting import (
    format_cost,
    format_count,
    format_provider_token_aggregate_lines,
    invocation_label,
)
from .models import (
    ProviderTokenAggregates,
    ProviderUsageRollup,
    RunSummary,
    WorkspaceInvocationBranchExportSummary,
    WorkspaceInvocationExecutionSummary,
    WorkspaceInvocationReuseSummary,
    WorkspaceInvocationSetupSummary,
    WorkspaceInvocationSummary,
    WorkspacePlanSummary,
)
from .spend import spend_overview_rows

_WORKSPACE_INVOCATION_LIMIT = 5


def render_run_summary_terminal(summary: RunSummary) -> str:
    """Render the concise terminal form of a run summary."""

    sections = [
        _terminal_run_overview_lines(summary),
        terminal_spend_lines(summary),
        terminal_workspace_lines(summary),
        terminal_issue_lines(summary),
        _terminal_artifact_lines(summary),
    ]
    return "\n\n".join("\n".join(section) for section in sections)


def _terminal_run_overview_lines(summary: RunSummary) -> list[str]:
    lines = [
        "Run Summary",
        f"  Workflow: {summary.workflow_name}",
        f"  Run ID: {summary.run_id}",
        f"  Status: {summary.workflow_status}",
    ]
    if summary.review_consensus_unresolved:
        lines.append("  Review consensus: unresolved; continued after exhaustion")
    lines.extend(
        [
            f"  Started: {summary.started_at}",
            f"  Completed: {summary.completed_at}",
        ]
    )
    if summary.elapsed_label is not None:
        lines.append(f"  Elapsed: {summary.elapsed_label}")
    lines.append(
        "  Nodes: "
        f"pending={summary.node_counts.pending} "
        f"running={summary.node_counts.running} "
        f"succeeded={summary.node_counts.succeeded} "
        f"blocked={summary.node_counts.blocked} "
        f"failed={summary.node_counts.failed}"
    )
    return lines


def _terminal_artifact_lines(summary: RunSummary) -> list[str]:
    return [
        "Artifacts",
        f"  Summary: {summary.summary_path}",
        f"  Events: {summary.event_log_path}",
    ]


def terminal_spend_lines(summary: RunSummary) -> list[str]:
    lines = ["Spend Observability"]
    lines.extend(_terminal_provider_token_lines(summary.provider_token_aggregates))
    if summary.spend is None:
        if summary.provider_token_aggregates.overall is None:
            lines.append("  No spend observability captured.")
        return lines

    lines.extend(
        f"  {row.label}: {row.terminal_value()}"
        for row in spend_overview_rows(summary.spend)
        if row.show_in_terminal
    )
    if summary.omitted_invocation_usage_count > 0:
        lines.append(
            "  Invocation detail: retained latest "
            f"{len(summary.invocation_usages)} invocation(s); "
            f"{summary.omitted_invocation_usage_count} earlier omitted"
        )
    if summary.provider_rollups:
        lines.append("  Providers:")
        for provider_rollup in summary.provider_rollups:
            lines.append(_terminal_provider_rollup_line(provider_rollup))
    return lines


def _terminal_provider_token_lines(aggregates: ProviderTokenAggregates) -> list[str]:
    if aggregates.overall is None:
        return []
    lines = [
        f"  {line}"
        for line in format_provider_token_aggregate_lines(aggregates.overall)
    ]
    for aggregate in aggregates.providers:
        if aggregate.provider is None:
            continue
        lines.append(f"  Provider {aggregate.provider}")
        lines.extend(
            f"    {line}" for line in format_provider_token_aggregate_lines(aggregate)
        )
    return lines


def _terminal_provider_rollup_line(rollup: ProviderUsageRollup) -> str:
    invocation_count = rollup.terminal_invocations
    details = [
        f"{invocation_count} invocation(s)",
        f"captured {rollup.cli_captured_invocations}/{invocation_count}",
        (
            "usage "
            f"{rollup.provider_usage_full_invocations}/{invocation_count} full, "
            f"{rollup.provider_usage_partial_invocations}/{invocation_count} partial, "
            f"{rollup.provider_usage_malformed_invocations}/{invocation_count} malformed"
        ),
        f"visible est {format_count(rollup.visible_estimate_tokens)} tokens",
        (
            "cost "
            f"{format_cost(rollup.configured_cost_usd)} "
            f"({rollup.configured_cost_confidence})"
        ),
    ]
    return f"    - {rollup.provider}: {'; '.join(details)}"


def terminal_workspace_lines(summary: RunSummary) -> list[str]:
    lines = ["Workspace Observability"]
    workspace = summary.workspace
    if workspace is None:
        lines.append("  Workspace isolation disabled or no workspace data captured.")
        return lines
    if workspace.plan is not None:
        lines.append(_terminal_workspace_plan_line(workspace.plan))
    lines.extend(_terminal_workspace_invocation_lines(workspace.invocations))
    return lines


def _terminal_workspace_plan_line(plan: WorkspacePlanSummary) -> str:
    contract = (
        f"{plan.worktree_contract_mode}:{plan.worktree_contract_schema_version}"
        if plan.worktree_contract_mode is not None
        else None
    )
    return (
        "  Plan: "
        f"contract={contract}; "
        f"source={plan.source_commit}; "
        f"invoker={plan.invoker_implementation}; "
        f"launch={plan.invoker_launch_mode}; "
        f"rendered_files={plan.rendered_locator_count}; "
        f"cleanup_on_success={plan.cleanup_on_success}"
    )


def _terminal_workspace_invocation_lines(
    invocations: Sequence[WorkspaceInvocationSummary],
) -> list[str]:
    if not invocations:
        return ["  No workspace invocations recorded."]
    lines = ["  Invocations:"]
    lines.extend(
        _terminal_workspace_invocation_line(invocation)
        for invocation in invocations[:_WORKSPACE_INVOCATION_LIMIT]
    )
    remaining_count = len(invocations) - _WORKSPACE_INVOCATION_LIMIT
    if remaining_count > 0:
        lines.append(f"    ... {remaining_count} more")
    return lines


def _terminal_workspace_invocation_line(invocation: WorkspaceInvocationSummary) -> str:
    label = invocation_label(
        node_id=invocation.node_id,
        task_id=invocation.task_id,
        audit_round_num=invocation.audit_round_num,
        round_num=invocation.round_num,
    )
    details = [
        *_workspace_source_result_details(invocation),
        *_workspace_setup_details(invocation.setup),
        *_workspace_reuse_details(invocation.reuse),
        *_workspace_snapshot_drift_details(invocation),
        *_workspace_execution_path_details(invocation.execution),
        *_workspace_branch_export_details(invocation.branch_export),
        *_workspace_input_details(invocation),
    ]
    return f"    - {label}: {'; '.join(details)}"


def _workspace_source_result_details(
    invocation: WorkspaceInvocationSummary,
) -> list[str]:
    details = [
        f"kind={invocation.workspace_kind}",
        f"worktree={invocation.logical_worktree_name}",
        f"status={invocation.status}",
        f"source={invocation.source.kind}:{invocation.source.commit}",
    ]
    if invocation.checkpoint_count is not None:
        details.append(f"checkpoints={invocation.checkpoint_count}")
    if invocation.result_commit is not None:
        details.append(f"result={invocation.result_commit}")
    if invocation.bundle_path is not None:
        details.append(f"bundle={invocation.bundle_path}")
    return details


def _workspace_setup_details(setup: WorkspaceInvocationSetupSummary) -> list[str]:
    if setup.status is None:
        return []
    label = setup.status
    if setup.profile_name is not None:
        label = f"{setup.profile_name}:{label}"
    return [f"setup={label}"]


def _workspace_reuse_details(reuse: WorkspaceInvocationReuseSummary) -> list[str]:
    details = []
    if reuse.strategy is not None:
        details.append(
            f"reuse={reuse.strategy},reused={reuse.reused},fallback={reuse.fallback}"
        )
    if reuse.reset_verification is not None:
        details.append(f"reset={reuse.reset_verification}")
    return details


def _workspace_snapshot_drift_details(
    invocation: WorkspaceInvocationSummary,
) -> list[str]:
    if invocation.snapshot_drift_discarded is None:
        return []
    return [
        "snapshot_drift="
        f"discarded={invocation.snapshot_drift_discarded},"
        f"changes={invocation.changed_path_count}"
    ]


def _workspace_execution_path_details(
    execution: WorkspaceInvocationExecutionSummary,
) -> list[str]:
    details = []
    if execution.cache_root is not None:
        details.append(f"cache_root={execution.cache_root}")
    if execution.effective_cwd is not None:
        details.append(f"cwd={execution.effective_cwd}")
    return details


def _workspace_branch_export_details(
    branch_export: WorkspaceInvocationBranchExportSummary,
) -> list[str]:
    if branch_export.status is None:
        return []
    return [
        "branch_export="
        f"{branch_export.status},"
        f"operation={branch_export.operation},"
        f"branch={branch_export.branch_name}"
    ]


def _workspace_input_details(invocation: WorkspaceInvocationSummary) -> list[str]:
    details = []
    if invocation.rendered_file_count is not None:
        details.append(f"rendered_files={invocation.rendered_file_count}")
    if invocation.child_environment_required is not None:
        details.append(
            "env="
            f"required={invocation.child_environment_required},"
            f"applied={invocation.child_environment_applied}"
        )
    return details


def terminal_issue_lines(summary: RunSummary) -> list[str]:
    lines = ["Warnings and Errors"]
    issue_count = len(summary.issues)
    if issue_count == 0:
        lines.append("  Count: 0")
        return lines
    lines.append(f"  Count: {issue_count}")
    for issue in summary.issues[:3]:
        lines.append(f"  - {issue.message}")
    remaining_issue_count = issue_count - 3
    if remaining_issue_count > 0:
        lines.append(f"  ... {remaining_issue_count} more")
    return lines
