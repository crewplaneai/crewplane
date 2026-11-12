from __future__ import annotations

import os
from unicodedata import category

from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text

from crewplane.observability.log_presentation.sanitize import sanitize_text
from crewplane.observability.text_layout import display_width, fit_text, wrap_text

from .formatting import (
    ISSUE_LIMIT,
    PROVIDER_ROW_LIMIT,
    RESULT_PATH_LIMIT,
    call_identity,
    format_call_duration,
    format_cost,
    format_count,
    format_duration,
    invocation_coverage_label,
    selected_stages,
    selected_timings,
    stage_counts_label,
    stage_status,
)
from .models import (
    InvocationTimingSummary,
    IssueSummary,
    NodeOutcomeSummary,
    ProviderTokenAggregate,
    ProviderUsageRollup,
    RunSummary,
)
from .terminal_table_layout import (
    CALL_HEADER,
    CELL_PADDING,
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

_STACKED_WIDTH = 40
_ISSUE_LINE_LIMIT = 3
_LABEL_LINE_LIMIT = 2


def render_run_summary_terminal(summary: RunSummary, console: Console) -> Group:
    """Build a bounded, literal presentation for the caller's output stream."""
    sections = (
        _overview(summary, console),
        _attention(summary, console),
        _stages(summary, console),
        _durations(summary, console),
        _usage(summary, console),
        _results(summary, console),
    )
    renderables: list[RenderableType] = []
    for section in sections:
        if section:
            if renderables:
                renderables.append(Text(""))
            renderables.extend(section)
    return Group(*renderables)


def _styled(console: Console) -> bool:
    return bool(
        console.file.isatty()
        and console.is_terminal
        and console.color_system is not None
        and not console.no_color
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "").lower() not in {"dumb", "unknown"}
    )


def _safe(value: object, console: Console) -> str:
    sanitized = " ".join(sanitize_text(value).splitlines()).replace("\t", " ")
    single_line = "".join(char for char in sanitized if category(char) != "Cc")
    return single_line.encode(console.encoding, errors="backslashreplace").decode(
        console.encoding
    )


def _text(value: object, console: Console, style: str = "") -> Text:
    return Text(_safe(value, console), style=style if _styled(console) else "")


def _wrapped(value: object, console: Console, style: str = "") -> Text:
    text = _text(value, console, style)
    text.plain = "\n".join(wrap_text(text.plain, console.width))
    return text


def _label(value: object, console: Console, style: str = "") -> Text:
    return _wrapped(
        fit_text(_safe(value, console), console.width * _LABEL_LINE_LIMIT),
        console,
        style,
    )


def _heading(label: str, console: Console) -> Text:
    return _label(label, console, "bold cyan")


def _omitted(count: int, subject: str, console: Console) -> list[RenderableType]:
    return (
        [_wrapped(f"{count} more {subject} in summary.md", console, "dim")]
        if count > 0
        else []
    )


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
        _heading(f"Run Summary: {summary.workflow_name}", console),
        _wrapped(
            f"Execution: {summary.workflow_status}    Elapsed: {format_duration(summary.elapsed_seconds)}",
            console,
        ),
        _wrapped(f"Stages: {stage_counts_label(summary)}", console),
        _wrapped(
            f"Calls: {calls}    Recorded attempts: {format_count(spend.total_attempts if spend else None)}",
            console,
        ),
        _wrapped(f"Usage coverage: {invocation_coverage_label(summary)}", console),
        _wrapped(f"Provider-reported tokens: {tokens}", console),
    ]
    if spend is not None and spend.configured_cost_usd is not None:
        lines.append(
            _wrapped(
                f"Configured cost estimate: {format_cost(spend.configured_cost_usd)} ({spend.configured_cost_confidence})",
                console,
            )
        )
    lines.append(_wrapped(f"Run ID: {summary.run_id}", console, "dim"))
    return lines


def _attention(summary: RunSummary, console: Console) -> list[RenderableType]:
    if not summary.issues and not summary.review_consensus_unresolved:
        return []
    lines: list[RenderableType] = [_heading("Needs attention", console)]
    if summary.review_consensus_unresolved:
        lines.append(
            _wrapped(
                "Review unresolved; execution continued after exhaustion or a no-progress stop.",
                console,
                "yellow",
            )
        )
    issues = sorted(summary.issues, key=lambda issue: issue.level != "error")
    lines.extend(_bounded_issue(issue, console) for issue in issues[:ISSUE_LIMIT])
    lines.extend(_omitted(len(issues) - ISSUE_LIMIT, "retained issues", console))
    return lines


def _bounded_issue(issue: IssueSummary, console: Console) -> Text:
    lines = wrap_text(_safe(issue.message, console), console.width)
    if len(lines) > _ISSUE_LINE_LIMIT:
        lines = lines[:_ISSUE_LINE_LIMIT]
        lines[-1] = fit_text(lines[-1] + "...", console.width)
        lines.append("Full issue in summary.md")
    return Text(
        "\n".join(lines),
        style=("red" if issue.level == "error" else "yellow")
        if _styled(console)
        else "",
    )


def _table(console: Console) -> Table:
    return Table(
        box=None,
        safe_box=True,
        padding=(0, CELL_PADDING),
        pad_edge=False,
        header_style="bold" if _styled(console) else "",
        border_style="",
        style="",
        show_edge=False,
        collapse_padding=True,
    )


def _stages(summary: RunSummary, console: Console) -> list[RenderableType]:
    nodes = selected_stages(summary)
    lines: list[RenderableType] = [_heading("Stages", console)]
    if not nodes:
        lines.append(
            _wrapped(
                "No stage state captured."
                if summary.node_counts is None
                else "No stages recorded.",
                console,
            )
        )
    elif console.width < _STACKED_WIDTH:
        for node in nodes:
            lines.append(_label(node.node_id, console))
            lines.extend(
                _status_duration(
                    stage_status(node), format_duration(node.duration_seconds), console
                )
            )
    else:
        lines.append(_stage_table(nodes, console))
    lines.extend(_omitted(len(summary.node_outcomes) - len(nodes), "stages", console))
    return lines


def _status_duration(
    status: str, duration: str, console: Console
) -> list[RenderableType]:
    value = f"{status} | {duration}"
    if display_width(_safe(value, console)) <= console.width:
        return [_text(value, console)]
    return [_wrapped(status, console), _wrapped(duration, console)]


def _stage_table(nodes: tuple[NodeOutcomeSummary, ...], console: Console) -> Table:
    table = _table(console)
    status_width = min(
        max(
            MIN_STATUS_WIDTH,
            *(display_width(_safe(stage_status(node), console)) for node in nodes),
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
            *(display_width(_safe(node.node_id, console)) for node in nodes),
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
            _text(fit_text(_safe(node.node_id, console), name_width), console),
            _text(stage_status(node), console),
            _text(format_duration(node.duration_seconds), console),
        )
    return table


def _durations(summary: RunSummary, console: Console) -> list[RenderableType]:
    timings = selected_timings(summary)
    if not timings:
        return []
    lines: list[RenderableType] = [
        _heading("Agent call durations", console),
        _wrapped(
            "Longest recorded calls; durations may overlap, not workflow elapsed time.",
            console,
        ),
    ]
    if console.width < _STACKED_WIDTH:
        for timing in timings:
            lines.extend(
                (
                    _label(call_identity(timing), console),
                    _label(_provider_model(timing), console),
                )
            )
            lines.extend(
                _status_duration(
                    timing.status, format_call_duration(timing.duration_ms), console
                )
            )
    else:
        lines.append(_duration_table(timings, console))
    lines.extend(
        _omitted(len(summary.invocation_timings) - len(timings), "agent calls", console)
    )
    return lines


def _provider_model(timing: InvocationTimingSummary) -> str:
    return f"{timing.provider} / {timing.model if timing.model is not None else 'model n/a'}"


def _duration_table(
    timings: tuple[InvocationTimingSummary, ...], console: Console
) -> Table:
    rows = _duration_rows(timings, console)
    layout = duration_table_layout(rows, console.width)
    table = _table(console)
    table.add_column(
        CALL_HEADER if layout.provider_width else COMBINED_CALL_HEADER,
        width=layout.label_width,
    )
    if layout.provider_width:
        table.add_column(PROVIDER_HEADER, width=layout.provider_width)
    table.add_column("Status", width=layout.status_width)
    table.add_column(
        DURATION_HEADER, width=layout.duration_width, justify="right", no_wrap=True
    )
    if layout.bar_width:
        table.add_column("", width=layout.bar_width)
    for row in rows:
        table.add_row(*_duration_row(row, layout, console))
    return table


def _duration_rows(
    timings: tuple[InvocationTimingSummary, ...], console: Console
) -> tuple[DurationTableRow, ...]:
    return tuple(
        DurationTableRow(
            _safe(call_identity(timing), console),
            _safe(_provider_model(timing), console),
            _safe(timing.status, console),
            format_call_duration(timing.duration_ms),
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
    cells.extend((_text(row.status, console), _text(row.duration, console)))
    if layout.bar_width:
        size = (
            max(
                1,
                round(
                    (row.duration_ms or 0)
                    / layout.maximum_duration_ms
                    * layout.bar_width
                ),
            )
            if row.duration_ms
            else 0
        )
        cells.append(_text("#" * size, console, "cyan"))
    return cells


def _usage(summary: RunSummary, console: Console) -> list[RenderableType]:
    spend = summary.spend
    aggregates = {
        aggregate.provider: aggregate
        for aggregate in summary.provider_token_aggregates.providers
    }
    rollups = {rollup.provider: rollup for rollup in summary.provider_rollups}
    providers = list(dict.fromkeys([*aggregates, *rollups]))
    lines: list[RenderableType] = []
    if spend is not None:
        estimate = (
            "n/a"
            if spend.visible_estimate_tokens is None
            else f"{format_count(spend.visible_estimate_tokens)} tokens (lower bound)"
        )
        lines.extend(
            (
                _heading("Usage details", console),
                _wrapped(f"Visible-text estimate: {estimate}", console),
            )
        )
    elif providers:
        lines.append(_heading("Usage details", console))
    for provider in providers[:PROVIDER_ROW_LIMIT]:
        rollup = rollups.get(provider) if provider is not None else None
        lines.extend(
            _provider_usage(provider, aggregates.get(provider), rollup, console)
        )
    lines.extend(_omitted(len(providers) - PROVIDER_ROW_LIMIT, "providers", console))
    if summary.omitted_invocation_usage_count:
        lines.append(
            _wrapped(
                f"{summary.omitted_invocation_usage_count} earlier usage rows not retained; full evidence in events.ndjson. Retained details in summary.md.",
                console,
            )
        )
    return lines


def _provider_usage(
    provider: str | None,
    aggregate: ProviderTokenAggregate | None,
    rollup: ProviderUsageRollup | None,
    console: Console,
) -> list[RenderableType]:
    lines: list[RenderableType] = [
        _label(provider or "unknown provider", console, "bold")
    ]
    if aggregate is not None:
        lines.append(_wrapped(_provider_tokens(aggregate), console))
    if rollup is not None:
        lines.append(_wrapped(_provider_coverage(rollup), console))
    return lines


def _provider_tokens(aggregate: ProviderTokenAggregate) -> str:
    buckets = (
        ("input", aggregate.input),
        ("cached input", aggregate.cached_input),
        ("output", aggregate.output),
        ("reasoning", aggregate.reasoning),
        ("cache write", aggregate.cache_write),
        ("total", aggregate.total),
    )
    return f"Reports: {aggregate.report_count}; " + "; ".join(
        f"{label}={format_count(value)}" for label, value in buckets
    )


def _provider_coverage(rollup: ProviderUsageRollup) -> str:
    return (
        f"{rollup.terminal_invocations} calls with usage facts; recorded attempts={rollup.total_attempts}; "
        f"CLI captured={rollup.cli_captured_invocations}; "
        f"usage full={rollup.provider_usage_full_invocations}, partial={rollup.provider_usage_partial_invocations}, malformed={rollup.provider_usage_malformed_invocations}; "
        f"cost={format_cost(rollup.configured_cost_usd)} ({rollup.configured_cost_confidence})"
    )


def _results(summary: RunSummary, console: Console) -> list[RenderableType]:
    nodes = [node for node in summary.node_outcomes if node.result_path is not None]
    lines: list[RenderableType] = [_heading("Results and reports", console)]
    for node in nodes[:RESULT_PATH_LIMIT]:
        label = fit_text(_safe(node.node_id, console), max(1, console.width // 3))
        lines.append(_wrapped(f"{label}: {node.result_path}", console))
    lines.extend(_omitted(len(nodes) - RESULT_PATH_LIMIT, "result paths", console))
    lines.extend(
        (
            _wrapped(f"Summary: {summary.summary_path}", console),
            _wrapped(f"Events: {summary.event_log_path}", console),
        )
    )
    if summary.workspace is not None:
        lines.append(_wrapped("Workspace details in summary.md", console))
    return lines
