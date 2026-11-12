"""Render run reports as LF-separated Markdown without writing artifacts.

Section renderers escape dynamic text and omit a trailing newline. Table cells
and details markup must already be prepared by callers.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from html import escape

from crewplane.architecture.contracts.run_summary import run_summary_header_lines
from crewplane.observability.log_presentation.sanitize import sanitize_text
from crewplane.observability.text_layout import fit_text, pad_text

from . import models as _models
from . import workspace_markdown as _workspace_markdown
from .formatting import (
    ISSUE_LIMIT,
    call_identity,
    format_call_duration,
    format_cost,
    format_count,
    format_duration,
    format_provider_tokens,
    format_visible_estimate,
    invocation_coverage_label,
    invocation_label,
    selected_stages,
    selected_timings,
    stage_counts_label,
    stage_status,
)
from .markdown_links import artifact_link, markdown_text
from .models import (
    InvocationTimingSummary,
    InvocationUsageSummary,
    NodeOutcomeSummary,
    ProviderTokenAggregate,
    RunSummary,
)
from .spend import spend_overview_rows

ATTENTION_MESSAGE_LIMIT = 400
TIMING_IDENTITY_WIDTH = 36
TIMING_PROVIDER_WIDTH = 28
TIMING_STATUS_WIDTH = 10
TIMING_BAR_WIDTH = 20


workspace_markdown_lines = _workspace_markdown.workspace_markdown_lines
workspace_rendered_file_label = _workspace_markdown.workspace_rendered_file_label
workspace_invocation_line = _workspace_markdown.workspace_invocation_line
workspace_source_label = _workspace_markdown.workspace_source_label
worktree_contract_label = _workspace_markdown.worktree_contract_label
plan_worktree_contract_label = _workspace_markdown.plan_worktree_contract_label
child_environment_label = _workspace_markdown.child_environment_label
workspace_execution_label = _workspace_markdown.workspace_execution_label
workspace_setup_label = _workspace_markdown.workspace_setup_label
workspace_reuse_label = _workspace_markdown.workspace_reuse_label
workspace_snapshot_drift_label = _workspace_markdown.workspace_snapshot_drift_label
workspace_branch_export_label = _workspace_markdown.workspace_branch_export_label
workspace_result_label = _workspace_markdown.workspace_result_label
workspace_bundle_label = _workspace_markdown.workspace_bundle_label
detail = _workspace_markdown.detail
bool_label = _workspace_markdown.bool_label
seconds_label = _workspace_markdown.seconds_label
joined_optional = _workspace_markdown.joined_optional
comma_optional = _workspace_markdown.comma_optional
compact = _workspace_markdown.compact
workspace_links = _workspace_markdown.workspace_links
WorkspaceInvocationSummary = _models.WorkspaceInvocationSummary
WorkspacePlanSummary = _models.WorkspacePlanSummary


def render_run_summary_markdown(summary: RunSummary) -> str:
    """Return a compact report with closed evidence details and one final LF."""
    header = "\n".join(
        run_summary_header_lines(
            summary.workflow_name, summary.run_id, summary.workflow_status
        )
    )
    sections = [
        header,
        "## Overview\n\n" + overview_markdown(summary),
        "## Needs attention\n\n" + attention_markdown(summary),
        "## Stages\n\n" + stages_markdown(summary, selected_stages(summary)),
        "## Spend Observability\n\n" + spend_markdown(summary),
        "## Agent call durations\n\n" + timing_chart_markdown(summary),
        "## Results and reports\n\n" + results_markdown(summary),
        details(
            "Invocation usage and timing details", invocation_details_markdown(summary)
        ),
        details(
            "Workspace Observability",
            "".join(workspace_markdown_lines(summary)) + workspace_links(summary),
        ),
    ]
    if len(summary.issues) > ISSUE_LIMIT:
        sections.append(
            details(
                "All retained issues",
                "".join(issue_markdown_lines(summary)),
                "retained-issues",
            )
        )
    if len(summary.node_outcomes) > len(selected_stages(summary)):
        sections.append(
            details(
                "Complete stage table",
                stages_markdown(summary, summary.node_outcomes),
                "all-stages",
            )
        )
    sections.append(
        details(
            "Artifact References", "".join(artifact_reference_markdown_lines(summary))
        )
    )
    return "\n\n".join(sections) + "\n"


def table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    """Join pre-escaped cells into a Markdown table without a trailing LF."""
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(row) + " |" for row in rows),
        ]
    )


def details(title: str, content: str, anchor: str | None = None) -> str:
    """Wrap trusted markup in closed details, stripping content's trailing space."""
    prefix = f'<a id="{anchor}"></a>\n\n' if anchor is not None else ""
    return f"{prefix}<details>\n<summary>{title}</summary>\n\n{content.rstrip()}\n\n</details>"


def overview_markdown(summary: RunSummary) -> str:
    """Render escaped overview values, using n/a for unavailable metrics."""
    overall = summary.provider_token_aggregates.overall
    spend = summary.spend
    tokens = (
        f"{format_count(overall.total)} across {overall.report_count} reports"
        if overall is not None
        else "n/a (no provider reports captured)"
    )
    values = [
        ("Elapsed", format_duration(summary.elapsed_seconds)),
        ("Started", summary.started_at),
        ("Completed", summary.completed_at),
        ("Stages", stage_counts_label(summary)),
        ("Calls observed", format_count(summary.invocation_count)),
        ("Recorded attempts", format_count(spend.total_attempts if spend else None)),
        ("Usage coverage", invocation_coverage_label(summary)),
        ("Provider-reported tokens", tokens),
        (
            "Visible-text estimate (lower-bound)",
            format_count(spend.visible_estimate_tokens if spend else None),
        ),
        (
            "Configured cost estimate",
            f"{format_cost(spend.configured_cost_usd)} (confidence: {spend.configured_cost_confidence})"
            if spend
            else "n/a (confidence: none)",
        ),
    ]
    return table(
        ("Metric", "Value"), [(label, markdown_text(value)) for label, value in values]
    )


def attention_markdown(summary: RunSummary) -> str:
    """Bound escaped issue previews while retaining complete long messages."""
    lines = []
    has_long_message = False
    if summary.review_consensus_unresolved:
        lines.append(
            "- Review consensus: unresolved; execution continued after consensus exhaustion or a no-progress stop."
        )
    for issue in summary.issues[:ISSUE_LIMIT]:
        message = markdown_text(issue.message)
        if len(message) > ATTENTION_MESSAGE_LIMIT:
            has_long_message = True
            message = (
                message[:ATTENTION_MESSAGE_LIMIT]
                + "... (complete message in retained issues below)"
            )
        lines.append(f"- {message}")
    omitted = len(summary.issues) - ISSUE_LIMIT
    if omitted > 0:
        lines.append(
            f"- {omitted} more retained issues in [all retained issues](#retained-issues)."
        )
    # Long messages need their full evidence even when the list itself is short.
    if has_long_message and omitted <= 0:
        lines.append(
            details("Complete retained issues", "".join(issue_markdown_lines(summary)))
        )
    return "\n".join(lines) if lines else "No warnings or errors recorded."


def stages_markdown(summary: RunSummary, nodes: tuple[NodeOutcomeSummary, ...]) -> str:
    """Render selected stages in supplied order, with escaped labels and links."""
    if not nodes:
        return (
            "No node state captured."
            if summary.node_counts is None
            else "No stages recorded."
        )
    rows = [
        (
            markdown_text(node.node_id),
            markdown_text(stage_status(node)),
            format_duration(node.duration_seconds),
            artifact_link(summary, node.result_path, "result")
            if node.result_path is not None
            else "not produced",
        )
        for node in nodes
    ]
    result = table(("Stage", "Execution", "Duration", "Result"), rows)
    omitted = len(summary.node_outcomes) - len(nodes)
    if omitted:
        result += (
            f"\n\n{omitted} more stages in the [complete stage table](#all-stages)."
        )
    return result


def spend_markdown(summary: RunSummary) -> str:
    """Render usage, token, and provider tables in order, then retention notes."""
    sections = [_spend_overview_markdown(summary)]
    if tokens := _provider_tokens_markdown(summary):
        sections.append(tokens)
    if summary.provider_rollups:
        sections.append(_provider_rollups_markdown(summary))
    sections.append(
        "Provider buckets are reported independently; cached input and reasoning are not added to totals. Visible-text estimates are separate lower bounds, not provider-reported usage."
    )
    if summary.omitted_invocation_usage_count:
        sections.append(invocation_usage_retention_line(summary))
    return "\n\n".join(sections)


def _spend_overview_markdown(summary: RunSummary) -> str:
    if summary.spend is None:
        return "No spend observability captured."
    return table(
        ("Usage metric", "Recorded value"),
        [
            (row.label, markdown_text(row.markdown_value()))
            for row in spend_overview_rows(summary.spend)
        ],
    )


def _provider_tokens_markdown(summary: RunSummary) -> str:
    aggregates = summary.provider_token_aggregates
    rows = [
        _provider_token_row(aggregate)
        for aggregate in (
            (aggregates.overall,) if aggregates.overall is not None else ()
        )
        + aggregates.providers
    ]
    if not rows:
        return ""
    return table(
        (
            "Provider",
            "Reports",
            "Input",
            "Cached input",
            "Cache write",
            "Output",
            "Reasoning",
            "Total",
        ),
        rows,
    )


def _provider_token_row(aggregate: ProviderTokenAggregate) -> tuple[str, ...]:
    return (
        markdown_text(aggregate.provider or "Overall"),
        str(aggregate.report_count),
        format_count(aggregate.input),
        format_count(aggregate.cached_input),
        format_count(aggregate.cache_write),
        format_count(aggregate.output),
        format_count(aggregate.reasoning),
        format_count(aggregate.total),
    )


def _provider_rollups_markdown(summary: RunSummary) -> str:
    return table(
        (
            "Provider",
            "Usage calls / recorded attempts",
            "CLI captured",
            "Full / partial / malformed",
            "Visible estimate (lower-bound)",
            "Configured cost / confidence",
        ),
        [
            (
                markdown_text(rollup.provider),
                f"{rollup.terminal_invocations} / {rollup.total_attempts}",
                str(rollup.cli_captured_invocations),
                f"{rollup.provider_usage_full_invocations} / {rollup.provider_usage_partial_invocations} / {rollup.provider_usage_malformed_invocations}",
                format_count(rollup.visible_estimate_tokens),
                f"{format_cost(rollup.configured_cost_usd)} / {rollup.configured_cost_confidence}",
            )
            for rollup in summary.provider_rollups
        ],
    )


def timing_chart_markdown(summary: RunSummary) -> str:
    """Render longest calls in a safe text fence; unknown durations have no bar."""
    rows = selected_timings(summary)
    if not rows:
        return "No invocation timing captured."
    longest = max((row.duration_ms or 0 for row in rows), default=0)
    chart = "\n".join(_timing_chart_line(row, longest) for row in rows)
    fence = "`" * max(
        3,
        max((len(match.group()) + 1 for match in re.finditer(r"`+", chart)), default=0),
    )
    result = f"Longest recorded calls. Individual durations may overlap; bars are not a workflow timeline or elapsed time.\n\n{fence}text\n{chart}\n{fence}"
    omitted = len(summary.invocation_timings) - len(rows)
    if omitted:
        result += f"\n\n{omitted} more calls in invocation timing details below."
    return result


def _timing_chart_line(row: InvocationTimingSummary, longest: int) -> str:
    identity = fit_text(
        " ".join(escape(sanitize_text(call_identity(row)), quote=False).splitlines()),
        TIMING_IDENTITY_WIDTH,
    )
    provider_model = fit_text(
        " ".join(
            escape(
                sanitize_text(f"{row.provider} / {row.model or 'n/a'}"), quote=False
            ).splitlines()
        ),
        TIMING_PROVIDER_WIDTH,
    )
    bar_length = (
        round(TIMING_BAR_WIDTH * row.duration_ms / longest)
        if longest and row.duration_ms is not None
        else 0
    )
    return f"{pad_text(identity, TIMING_IDENTITY_WIDTH)}  {pad_text(provider_model, TIMING_PROVIDER_WIDTH)}  {pad_text(row.status, TIMING_STATUS_WIDTH)}  {pad_text('#' * bar_length, TIMING_BAR_WIDTH)} {format_call_duration(row.duration_ms)}"


def results_markdown(summary: RunSummary) -> str:
    """List available results in stage order, followed by report and event links."""
    lines = [
        f"- {markdown_text(node.node_id)}: {artifact_link(summary, node.result_path, 'result')}"
        for node in summary.node_outcomes
        if node.result_path is not None
    ]
    lines.extend(
        (
            f"- {artifact_link(summary, summary.summary_path, 'summary.md')}",
            f"- {artifact_link(summary, summary.event_log_path, 'events.ndjson')}",
        )
    )
    return "\n".join(lines)


def invocation_usage_retention_line(summary: RunSummary) -> str:
    """Return a single-line retention notice with a report-relative event link."""
    return (
        f"Invocation detail: retained latest {len(summary.invocation_usages)} invocation(s); "
        f"{summary.omitted_invocation_usage_count} earlier invocation detail(s) were omitted from summary detail. "
        f"Full invocation events remain in {artifact_link(summary, summary.event_log_path, 'events.ndjson')}."
    )


def invocation_details_markdown(summary: RunSummary) -> str:
    """Render escaped usage records then timing rows in their retained order."""
    lines = []
    if summary.omitted_invocation_usage_count:
        lines.append(invocation_usage_retention_line(summary) + "\n")
    lines.extend(_invocation_usage_line(usage) for usage in summary.invocation_usages)
    if not summary.invocation_usages:
        lines.append("No invocation usage details captured.")
    if summary.invocation_timings:
        lines.append("\n" + _invocation_timing_table(summary))
    return "\n".join(lines)


def _invocation_usage_line(usage: InvocationUsageSummary) -> str:
    label = invocation_label(
        usage.node_id, usage.task_id, usage.audit_round_num, usage.round_num
    )
    values = [
        f"provider: {usage.provider}",
        f"captured: {'yes' if usage.cli_captured else 'no'}",
        f"output extraction: {usage.output_extraction_status}",
        f"provider report: {usage.provider_usage_status}",
        f"attempts: {usage.attempt_count}",
        f"visible estimate: {format_visible_estimate(usage)}",
        f"configured cost: {format_cost(usage.configured_cost_usd)}",
        f"confidence: {usage.invocation_cost_confidence}",
        f"provider report count: {format_count(usage.provider_usage_report_count)}",
        f"provider tokens: {format_provider_tokens(usage.provider_tokens)}",
    ]
    for key, value in (
        ("usage parse error", usage.usage_parse_error),
        ("failure", usage.failure_kind),
        ("failure phase", usage.failure_phase),
        ("failure source", usage.failure_source),
        ("failure advice", usage.failure_advice),
    ):
        if value is not None:
            values.append(f"{key}: {value}")
    return f"- {markdown_text(label)}: {markdown_text('; '.join(values))}"


def _invocation_timing_table(summary: RunSummary) -> str:
    return table(
        ("Invocation", "Provider", "Recorded model", "Execution", "Duration"),
        [
            (
                markdown_text(call_identity(timing)),
                markdown_text(timing.provider),
                markdown_text(timing.model or "n/a"),
                markdown_text(timing.status),
                format_call_duration(timing.duration_ms),
            )
            for timing in summary.invocation_timings
        ],
    )


def issue_markdown_lines(summary: RunSummary) -> list[str]:
    """Return escaped issue bullets, each ending in LF, or an empty-list notice."""
    if not summary.issues:
        return ["No warnings or errors recorded.\n"]
    return [f"- {markdown_text(issue.message)}\n" for issue in summary.issues]


def artifact_reference_markdown_lines(summary: RunSummary) -> list[str]:
    """Return invocation artifact bullets in retained order, each ending in LF."""
    if not summary.artifact_references:
        return ["No invocation artifacts captured.\n"]
    lines = []
    for reference in summary.artifact_references:
        label = invocation_label(
            reference.node_id,
            reference.task_id,
            reference.audit_round_num,
            reference.round_num,
        )
        parts = [markdown_text(label)]
        if reference.output_file is not None:
            parts.append(artifact_link(summary, reference.output_file, "output"))
        if reference.log_file is not None:
            parts.append(artifact_link(summary, reference.log_file, "log"))
        lines.append(f"- {'; '.join(parts)}\n")
    return lines
