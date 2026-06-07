from __future__ import annotations

from .formatting import (
    format_cost,
    format_provider_tokens,
    format_visible_estimate,
    invocation_label,
)
from .models import RunSummary
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
