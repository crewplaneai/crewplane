from __future__ import annotations

from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text

from crewplane.observability.text_layout import display_width, fit_text, wrap_text

from .formatting import (
    ISSUE_LIMIT,
    RESULT_PATH_LIMIT,
    call_identity,
    format_chart_duration,
    format_cost,
    format_count,
    format_duration,
    full_track,
    invocation_coverage_label,
    selected_stages,
    selected_timings,
    stage_counts_label,
    stage_status,
    timing_footer,
    timing_model,
)
from .models import (
    InvocationTimingSummary,
    IssueSummary,
    NodeOutcomeSummary,
    RunSummary,
)
from .terminal_support import heading, label, omitted, safe, styled, text, wrapped
from .terminal_support import table as summary_table
from .terminal_table_layout import (
    CALL_HEADER,
    COLUMN_PADDING,
    COMBINED_CALL_HEADER,
    DURATION_HEADER,
    EXECUTION_HEADER,
    MIN_STATUS_WIDTH,
    PROVIDER_HEADER,
    DurationTableLayout,
    DurationTableRow,
    duration_table_layout,
)
from .terminal_tokens import token_sections

_STACKED_WIDTH = 40
_ISSUE_LINE_LIMIT = 3


def render_run_summary_terminal(summary: RunSummary, console: Console) -> Group:
    """Build a bounded, literal presentation for the caller's output stream."""
    sections = (
        _overview(summary, console),
        _attention(summary, console),
        _stages(summary, console),
        _durations(summary, console),
        token_sections(summary, console),
        _results(summary, console),
    )
    renderables: list[RenderableType] = []
    for section in sections:
        if section:
            if renderables:
                renderables.append(Text(""))
            renderables.extend(section)
    return Group(*renderables)


def _overview(summary: RunSummary, console: Console) -> list[RenderableType]:
    spend = summary.spend
    calls = format_count(summary.invocation_count)
    if summary.invocation_count == 0:
        calls += " (no new calls)"
    aggregate = summary.provider_token_aggregates.overall
    tokens = (
        "n/a"
        if aggregate is None
        else f"{format_count(aggregate.total)} across {aggregate.report_count} reports"
    )
    lines: list[RenderableType] = [
        heading(f"Run Summary: {summary.workflow_name}", console),
        wrapped(
            f"Execution: {summary.workflow_status}    Elapsed: {format_duration(summary.elapsed_seconds)}",
            console,
        ),
        wrapped(f"Stages: {stage_counts_label(summary)}", console),
        wrapped(
            f"Calls: {calls}    Recorded attempts: {format_count(spend.total_attempts if spend else None)}",
            console,
        ),
        wrapped(f"Usage coverage: {invocation_coverage_label(summary)}", console),
        wrapped(f"Provider-reported tokens: {tokens}", console),
    ]
    if spend is not None and spend.configured_cost_usd is not None:
        lines.append(
            wrapped(
                f"Configured cost estimate: {format_cost(spend.configured_cost_usd)} ({spend.configured_cost_confidence})",
                console,
            )
        )
    lines.append(wrapped(f"Run ID: {summary.run_id}", console, "dim"))
    return lines


def _attention(summary: RunSummary, console: Console) -> list[RenderableType]:
    if not summary.issues and not summary.review_consensus_unresolved:
        return []
    lines: list[RenderableType] = [heading("Needs attention", console)]
    if summary.review_consensus_unresolved:
        lines.append(
            wrapped(
                "Review unresolved; execution continued after exhaustion or a no-progress stop.",
                console,
                "yellow",
            )
        )
    issues = sorted(summary.issues, key=lambda issue: issue.level != "error")
    lines.extend(_bounded_issue(issue, console) for issue in issues[:ISSUE_LIMIT])
    lines.extend(omitted(len(issues) - ISSUE_LIMIT, "retained issues", console))
    return lines


def _bounded_issue(issue: IssueSummary, console: Console) -> Text:
    lines = wrap_text(safe(issue.message, console), console.width)
    if len(lines) > _ISSUE_LINE_LIMIT:
        lines = lines[:_ISSUE_LINE_LIMIT]
        lines[-1] = fit_text(lines[-1] + "...", console.width)
        lines.append("Full issue in summary.md")
    return Text(
        "\n".join(lines),
        style=("red" if issue.level == "error" else "yellow")
        if styled(console)
        else "",
    )


def _stages(summary: RunSummary, console: Console) -> list[RenderableType]:
    nodes = selected_stages(summary)
    lines: list[RenderableType] = [heading("Stages", console)]
    if not nodes:
        lines.append(
            wrapped(
                "No stage state captured."
                if summary.node_counts is None
                else "No stages recorded.",
                console,
            )
        )
    elif console.width < _STACKED_WIDTH:
        for node in nodes:
            lines.append(label(node.node_id, console))
            lines.extend(
                _status_duration(
                    stage_status(node), format_duration(node.duration_seconds), console
                )
            )
    else:
        lines.append(_stage_table(nodes, console))
    lines.extend(omitted(len(summary.node_outcomes) - len(nodes), "stages", console))
    return lines


def _status_duration(
    status: str, duration: str, console: Console
) -> list[RenderableType]:
    value = f"{status} | {duration}"
    if display_width(safe(value, console)) <= console.width:
        return [text(value, console)]
    return [wrapped(status, console), wrapped(duration, console)]


def _stage_table(nodes: tuple[NodeOutcomeSummary, ...], console: Console) -> Table:
    table = summary_table(console)
    status_width = min(
        max(
            MIN_STATUS_WIDTH,
            *(display_width(safe(stage_status(node), console)) for node in nodes),
        ),
        console.width // 2,
    )
    duration_width = max(
        display_width(DURATION_HEADER),
        *(display_width(format_duration(node.duration_seconds)) for node in nodes),
    )
    stage_header = "Stage"
    name_width = min(
        max(
            display_width(stage_header),
            *(display_width(safe(node.node_id, console)) for node in nodes),
        ),
        max(1, console.width - status_width - duration_width - 2 * COLUMN_PADDING),
    )
    table.add_column(stage_header, width=name_width)
    table.add_column(EXECUTION_HEADER, width=status_width)
    table.add_column(
        DURATION_HEADER, width=duration_width, justify="right", no_wrap=True
    )
    for node in nodes:
        table.add_row(
            text(fit_text(safe(node.node_id, console), name_width), console),
            text(stage_status(node), console),
            text(format_duration(node.duration_seconds), console),
        )
    return table


def _durations(summary: RunSummary, console: Console) -> list[RenderableType]:
    timings = selected_timings(summary)
    if not timings:
        return []
    lines: list[RenderableType] = [
        heading("Agent call durations", console),
        wrapped(
            "Logical calls include retries/waits and may overlap; exclude workspace preparation and later publication. Cancellation ends when recorded.",
            console,
        ),
    ]
    if duration_table_layout(_duration_rows(timings, console), console.width).stacked:
        for timing in timings:
            lines.extend(
                (
                    label(call_identity(timing), console),
                    label(_provider_model(timing), console),
                )
            )
            lines.extend(
                _status_duration(
                    timing.status, format_chart_duration(timing.duration_ms), console
                )
            )
    else:
        lines.append(_duration_table(timings, console))
    lines.extend(
        omitted(len(summary.invocation_timings) - len(timings), "agent calls", console)
    )
    lines.append(wrapped(timing_footer(summary), console))
    lines.append(wrapped("Full identities and exact durations in summary.md.", console))
    if all(timing.duration_ms is None for timing in summary.invocation_timings):
        lines.append(wrapped("No usable duration measurements recorded.", console))
    elif not any(timing.duration_ms for timing in summary.invocation_timings):
        lines.append(wrapped("No positive duration recorded.", console))
    return lines


def _provider_model(timing: InvocationTimingSummary) -> str:
    return timing_model(timing)


def _duration_table(
    timings: tuple[InvocationTimingSummary, ...], console: Console
) -> Table:
    rows = _duration_rows(timings, console)
    layout = duration_table_layout(rows, console.width)
    table = summary_table(console)
    table.add_column(
        CALL_HEADER if layout.provider_width else COMBINED_CALL_HEADER,
        width=layout.label_width,
    )
    if layout.provider_width:
        table.add_column(PROVIDER_HEADER, width=layout.provider_width)
    table.add_column("Status", width=layout.status_width)
    if layout.bar_width:
        table.add_column("Relative runtime", width=layout.bar_width, no_wrap=True)
    table.add_column(
        DURATION_HEADER, width=layout.duration_width, justify="right", no_wrap=True
    )
    for row in rows:
        table.add_row(*_duration_row(row, layout, console))
    return table


def _duration_rows(
    timings: tuple[InvocationTimingSummary, ...], console: Console
) -> tuple[DurationTableRow, ...]:
    return tuple(
        DurationTableRow(
            safe(call_identity(timing), console),
            safe(_provider_model(timing), console),
            safe(timing.status, console),
            format_chart_duration(timing.duration_ms),
            timing.duration_ms,
        )
        for timing in timings
    )


def _duration_row(
    row: DurationTableRow, layout: DurationTableLayout, console: Console
) -> list[Text]:
    label = fit_text(row.identity, layout.label_width)
    cells = (
        [Text(label)]
        if layout.provider_width
        else [Text(f"{label}\n{fit_text(row.provider_model, layout.label_width)}")]
    )
    if layout.provider_width:
        cells.append(Text(fit_text(row.provider_model, layout.provider_width)))
    cells.append(text(row.status, console))
    if layout.bar_width:
        cells.append(
            text(
                full_track(
                    row.duration_ms, layout.maximum_duration_ms, layout.bar_width
                ),
                console,
                "cyan",
            )
        )
    cells.append(text(row.duration, console))
    return cells


def _results(summary: RunSummary, console: Console) -> list[RenderableType]:
    nodes = [node for node in summary.node_outcomes if node.result_path is not None]
    lines: list[RenderableType] = [heading("Results and reports", console)]
    for node in nodes[:RESULT_PATH_LIMIT]:
        label = fit_text(safe(node.node_id, console), max(1, console.width // 3))
        lines.append(wrapped(f"{label}: {node.result_path}", console))
    lines.extend(omitted(len(nodes) - RESULT_PATH_LIMIT, "result paths", console))
    lines.extend(
        (
            wrapped(f"Summary: {summary.summary_path}", console),
            wrapped(f"Events: {summary.event_log_path}", console),
        )
    )
    if summary.workspace is not None:
        lines.append(wrapped("Workspace details in summary.md", console))
    return lines
