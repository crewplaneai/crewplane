from __future__ import annotations

from .formatting import format_cost, format_count
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
