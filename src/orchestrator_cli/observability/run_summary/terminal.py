from __future__ import annotations

from .formatting import format_cost, format_count, invocation_label
from .models import RunSummary
from .spend import spend_overview_rows


def render_run_summary_terminal(summary: RunSummary) -> str:
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
    lines.append("")
    lines.extend(terminal_spend_lines(summary))
    lines.append("")
    lines.extend(terminal_workspace_lines(summary))
    lines.append("")
    lines.extend(terminal_issue_lines(summary))
    lines.append("")
    lines.append("Artifacts")
    lines.append(f"  Summary: {summary.summary_path}")
    lines.append(f"  Events: {summary.event_log_path}")
    return "\n".join(lines)


def terminal_spend_lines(summary: RunSummary) -> list[str]:
    lines = ["Spend Observability"]
    if summary.spend is None:
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
            details = [
                f"{provider_rollup.terminal_invocations} invocation(s)",
                (
                    "captured "
                    f"{provider_rollup.cli_captured_invocations}/"
                    f"{provider_rollup.terminal_invocations}"
                ),
                (
                    "reports "
                    f"{provider_rollup.provider_usage_full_invocations}/"
                    f"{provider_rollup.terminal_invocations} full, "
                    f"{provider_rollup.provider_usage_partial_invocations}/"
                    f"{provider_rollup.terminal_invocations} partial, "
                    f"{provider_rollup.provider_usage_malformed_invocations}/"
                    f"{provider_rollup.terminal_invocations} malformed"
                ),
                (
                    "visible est "
                    f"{format_count(provider_rollup.visible_estimate_tokens)} tokens"
                ),
                (
                    "cost "
                    f"{format_cost(provider_rollup.configured_cost_usd)} "
                    f"({provider_rollup.configured_cost_confidence})"
                ),
            ]
            lines.append(f"    - {provider_rollup.provider}: {'; '.join(details)}")
    return lines


def terminal_workspace_lines(summary: RunSummary) -> list[str]:
    lines = ["Workspace Observability"]
    workspace = summary.workspace
    if workspace is None:
        lines.append("  Workspace isolation disabled or no workspace data captured.")
        return lines
    plan = workspace.plan
    if plan is not None:
        contract = (
            f"{plan.worktree_contract_mode}:{plan.worktree_contract_schema_version}"
            if plan.worktree_contract_mode is not None
            else None
        )
        lines.append(
            "  Plan: "
            f"contract={contract}; "
            f"source={plan.source_commit}; "
            f"invoker={plan.invoker_implementation}; "
            f"launch={plan.invoker_launch_mode}; "
            f"rendered_files={plan.rendered_locator_count}; "
            f"cleanup_on_success={plan.cleanup_on_success}"
        )
    if not workspace.invocations:
        lines.append("  No workspace invocations recorded.")
        return lines
    lines.append("  Invocations:")
    for invocation in workspace.invocations[:5]:
        source = invocation.source
        execution = invocation.execution
        setup = invocation.setup
        reuse = invocation.reuse
        branch_export = invocation.branch_export
        label = invocation_label(
            node_id=invocation.node_id,
            task_id=invocation.task_id,
            audit_round_num=invocation.audit_round_num,
            round_num=invocation.round_num,
        )
        details = [
            f"kind={invocation.workspace_kind}",
            f"worktree={invocation.logical_worktree_name}",
            f"status={invocation.status}",
            f"source={source.kind}:{source.commit}",
        ]
        if invocation.checkpoint_count is not None:
            details.append(f"checkpoints={invocation.checkpoint_count}")
        if invocation.result_commit is not None:
            details.append(f"result={invocation.result_commit}")
        if invocation.bundle_path is not None:
            details.append(f"bundle={invocation.bundle_path}")
        if setup.status is not None:
            setup_label = f"{setup.profile_name}:{setup.status}"
            if setup.profile_name is None:
                setup_label = setup.status
            details.append(f"setup={setup_label}")
        if reuse.strategy is not None:
            details.append(
                "reuse="
                f"{reuse.strategy},"
                f"reused={reuse.reused},"
                f"fallback={reuse.fallback}"
            )
        if reuse.reset_verification is not None:
            details.append(f"reset={reuse.reset_verification}")
        if invocation.snapshot_drift_discarded is not None:
            details.append(
                "snapshot_drift="
                f"discarded={invocation.snapshot_drift_discarded},"
                f"changes={invocation.changed_path_count}"
            )
        if execution.cache_root is not None:
            details.append(f"cache_root={execution.cache_root}")
        if execution.effective_cwd is not None:
            details.append(f"cwd={execution.effective_cwd}")
        if branch_export.status is not None:
            details.append(
                "branch_export="
                f"{branch_export.status},"
                f"operation={branch_export.operation},"
                f"branch={branch_export.branch_name}"
            )
        if invocation.rendered_file_count is not None:
            details.append(f"rendered_files={invocation.rendered_file_count}")
        if invocation.child_environment_required is not None:
            details.append(
                "env="
                f"required={invocation.child_environment_required},"
                f"applied={invocation.child_environment_applied}"
            )
        lines.append(f"    - {label}: {'; '.join(details)}")
    remaining_count = len(workspace.invocations) - 5
    if remaining_count > 0:
        lines.append(f"    ... {remaining_count} more")
    return lines


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
