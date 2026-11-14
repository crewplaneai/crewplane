"""Reported-token comparisons, independent from timing and cost estimates."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console, RenderableType
from rich.text import Text

from crewplane.observability.text_layout import display_width, fit_text

from .formatting import (
    format_cost,
    format_count,
    full_track,
    node_coverage,
    selected_nodes,
    selected_providers,
)
from .models import (
    NodeTokenAggregate,
    ProviderTokenAggregate,
    ProviderUsageRollup,
    RunSummary,
)
from .terminal_support import heading, label, omitted, safe, table, text, wrapped
from .terminal_table_layout import COLUMN_PADDING

_WIDE_NODE_WIDTH = 100
_BAR_MIN_CONSOLE_WIDTH = 60
_STACKED_WIDTH = 40
_MIN_IDENTITY_WIDTH = 8
_MIN_BAR_WIDTH = 12
_MAX_BAR_WIDTH = 30


@dataclass(frozen=True)
class _TokenComparisonRow:
    identity: str
    cells: tuple[str, ...]
    total: int | None


def token_sections(summary: RunSummary, console: Console) -> list[RenderableType]:
    """Show two non-additive groupings of current-run reported usage."""
    lines = _nodes(summary, console) + _providers(summary, console)
    lines.append(
        wrapped(
            "Node and provider rows are two views of the same reports; do not add them together. Reported totals exclude unreported usage.",
            console,
        )
    )
    if summary.spend is not None:
        spend = summary.spend
        lines.append(
            wrapped(
                f"Visible-text estimate: {format_count(spend.visible_estimate_tokens)} tokens (lower bound); separate from reported tokens.",
                console,
            )
        )
        lines.append(
            wrapped(
                f"Configured cost estimate: {format_cost(spend.configured_cost_usd)} ({spend.configured_cost_confidence})",
                console,
            )
        )
    if summary.omitted_invocation_usage_count:
        lines.append(
            wrapped(
                f"{summary.omitted_invocation_usage_count} earlier usage rows not retained; full evidence in events.ndjson. Retained details in summary.md.",
                console,
            )
        )
    return lines


def _nodes(summary: RunSummary, console: Console) -> list[RenderableType]:
    nodes = selected_nodes(summary)
    maximum = max(
        (
            node.total
            for node in summary.node_token_aggregates
            if node.total is not None
        ),
        default=0,
    )
    lines: list[RenderableType] = [heading("Reported tokens by node", console)]
    if nodes:
        lines.extend(_node_chart(nodes, maximum, console))
    else:
        lines.append(wrapped("No exact node usage available.", console))
    lines.extend(_node_reporting_coverage(summary, len(nodes), maximum, console))
    for node in nodes:
        lines.extend(_node_evidence(node, console))
    if summary.unattributed_node_tokens is not None:
        lines.extend(_unattributed_node(summary.unattributed_node_tokens, console))
    if nodes and all(node.total is None for node in summary.node_token_aggregates):
        lines.append(wrapped("No usable reported node totals recorded.", console))
    elif nodes and maximum == 0:
        lines.append(wrapped("No positive node total recorded.", console))
    lines.append(
        wrapped(
            "Calls w/reports counts calls with at least one valid report, not attempt coverage. All tasks and review rounds are included; only this run's reports count. Full identities and coverage in summary.md.",
            console,
        )
    )
    return lines


def _node_reporting_coverage(
    summary: RunSummary, shown: int, maximum: int, console: Console
) -> list[RenderableType]:
    unknown = sum(node.total is None for node in summary.node_token_aggregates)
    lines: list[RenderableType] = [
        wrapped(
            f"Scale: largest node total = {format_count(maximum) if len(summary.node_token_aggregates) > unknown else 'n/a'} reported tokens. {shown} shown of {len(summary.node_token_aggregates)} nodes; {unknown} unknown totals.",
            console,
        )
    ]
    incomplete = sum(
        not node.exact
        or node.observed_calls is None
        or node.calls_with_reports is None
        or node.calls_with_reports != node.observed_calls
        or bool(node.missing_attempt_count_calls or node.missing_report_count_calls)
        for node in summary.node_token_aggregates
    )
    if incomplete:
        lines.append(
            wrapped(
                f"{incomplete} nodes have incomplete or unknown reporting-call/attempt coverage, including omitted nodes. See complete coverage in summary.md.",
                console,
            )
        )
    lines.extend(
        omitted(
            len(summary.node_token_aggregates) - shown,
            "nodes and complete bucket/coverage details",
            console,
        )
    )
    return lines


def _unattributed_node(
    node: NodeTokenAggregate, console: Console
) -> list[RenderableType]:
    lines: list[RenderableType] = [heading("Unattributed (node ID missing)", console)]
    lines.extend(
        _labeled_values(
            (
                ("Input", format_count(node.input)),
                ("Output", format_count(node.output)),
                ("Total", format_count(node.total)),
                ("Reports", format_count(node.report_count)),
                ("Calls w/reports", node_coverage(node)),
            ),
            console,
        )
    )
    lines.extend(_node_evidence(node, console))
    return lines


def _node_chart(
    nodes: tuple[NodeTokenAggregate, ...], maximum: int, console: Console
) -> list[RenderableType]:
    wide = console.width >= _WIDE_NODE_WIDTH
    headers = (("Input", "Output") if wide else ()) + (
        "Total",
        "Reports",
        "Calls w/reports",
    )
    rows = tuple(
        _TokenComparisonRow(
            safe(node.node_id, console),
            ((format_count(node.input), format_count(node.output)) if wide else ())
            + (
                format_count(node.total),
                format_count(node.report_count),
                node_coverage(node),
            ),
            node.total,
        )
        for node in nodes
    )
    rendered = _comparison(rows, headers, maximum, console, "Node")
    if not wide:
        for node in nodes:
            rendered.append(label(node.node_id, console))
            rendered.extend(
                _labeled_values(
                    (
                        ("Input", format_count(node.input)),
                        ("Output", format_count(node.output)),
                    ),
                    console,
                )
            )
    return rendered


def _comparison(
    rows: tuple[_TokenComparisonRow, ...],
    headers: tuple[str, ...],
    maximum: int,
    console: Console,
    identity_header: str,
) -> list[RenderableType]:
    widths = [
        max(display_width(header), *(display_width(row.cells[index]) for row in rows))
        for index, header in enumerate(headers)
    ]
    fixed = sum(widths) + COLUMN_PADDING * len(widths)
    natural = max(
        display_width(identity_header), *(display_width(row.identity) for row in rows)
    )
    bars = console.width >= _BAR_MIN_CONSOLE_WIDTH and any(
        row.total is not None for row in rows
    )
    bar = (
        min(
            _MAX_BAR_WIDTH,
            max(_MIN_BAR_WIDTH, console.width - fixed - natural - COLUMN_PADDING),
        )
        if bars
        else 0
    )
    available = console.width - fixed - (bar + COLUMN_PADDING if bar else 0)
    if available < _MIN_IDENTITY_WIDTH and bar:
        bar = 0
        available = console.width - fixed
    if console.width < _STACKED_WIDTH or available < _MIN_IDENTITY_WIDTH:
        lines: list[RenderableType] = []
        for row in rows:
            lines.append(label(row.identity, console))
            lines.extend(
                _labeled_values(tuple(zip(headers, row.cells, strict=True)), console)
            )
        return lines
    label_width = min(natural, available)
    chart = table(console)
    chart.add_column(identity_header, width=label_width, no_wrap=True)
    if bar:
        chart.add_column("Relative total", width=bar, no_wrap=True)
    for header, width in zip(headers, widths, strict=True):
        chart.add_column(header, width=width, justify="right", no_wrap=True)
    for row in rows:
        cells = [text(fit_text(row.identity, label_width), console)]
        if bar:
            cells.append(text(full_track(row.total, maximum, bar), console, "cyan"))
        cells.extend(text(value, console) for value in row.cells)
        chart.add_row(*cells)
    return [chart]


def _node_evidence(node: NodeTokenAggregate, console: Console) -> list[RenderableType]:
    facts = []
    if node.resume_kind:
        facts.append(
            f"{node.resume_kind} result; prior-run consumption is not copied here"
        )
    if node.status:
        facts.append(f"status {node.status}")
    if node.zero_report_calls:
        facts.append(f"{node.zero_report_calls} calls with zero reports")
    if node.missing_report_count_calls:
        facts.append(f"{node.missing_report_count_calls} calls missing report counts")
    if node.missing_attempt_count_calls:
        facts.append(f"{node.missing_attempt_count_calls} calls missing attempt counts")
    if node.calls_without_terminal:
        facts.append(
            f"{node.calls_without_terminal} observed calls without terminal usage"
        )
    if node.usage_partial_calls or node.usage_malformed_calls or node.parse_error_calls:
        facts.append(
            f"partial {node.usage_partial_calls}, malformed {node.usage_malformed_calls}, parse errors {node.parse_error_calls}"
        )
    if node.observed_calls is None:
        facts.append("call coverage unknown")
    return (
        [
            wrapped(
                f"{fit_text(safe(node.node_id or 'Unattributed', console), console.width)}: {', '.join(facts)}.",
                console,
            )
        ]
        if facts
        else []
    )


def _providers(summary: RunSummary, console: Console) -> list[RenderableType]:
    aggregates = {
        item.provider: item for item in summary.provider_token_aggregates.providers
    }
    rollups = {item.provider: item for item in summary.provider_rollups}
    providers = selected_providers(summary)
    maximum = max(
        (item.total for item in aggregates.values() if item.total is not None),
        default=0,
    )
    overall = summary.provider_token_aggregates.overall
    lines: list[RenderableType] = [
        Text(""),
        heading("Provider-reported tokens", console),
        wrapped(
            f"Overall reported total: {format_count(overall.total if overall else None)} across {format_count(overall.report_count if overall else None)} reports.",
            console,
        ),
    ]
    if providers:
        lines.extend(_provider_chart(providers, aggregates, maximum, console))
    scale = (
        format_count(maximum)
        if any(item.total is not None for item in aggregates.values())
        else "n/a"
    )
    lines.append(
        wrapped(f"Scale: largest provider total = {scale} reported tokens.", console)
    )
    lines.extend(
        omitted(
            len(set(aggregates) | set(rollups)) - len(providers),
            "providers and complete bucket/coverage details",
            console,
        )
    )
    for alias in providers:
        rollup = rollups.get(alias) if alias is not None else None
        lines.extend(_provider_details(alias, aggregates.get(alias), rollup, console))
    if providers and not any(item.total is not None for item in aggregates.values()):
        lines.append(wrapped("No usable reported provider totals recorded.", console))
    elif providers and maximum == 0:
        lines.append(wrapped("No positive provider total recorded.", console))
    return lines


def _provider_chart(
    providers: tuple[str | None, ...],
    aggregates: dict[str | None, ProviderTokenAggregate],
    maximum: int,
    console: Console,
) -> list[RenderableType]:
    rows: list[_TokenComparisonRow] = []
    for alias in providers:
        aggregate = aggregates.get(alias)
        total = aggregate.total if aggregate is not None else None
        rows.append(
            _TokenComparisonRow(
                safe(alias or "unknown provider", console),
                (
                    format_count(total),
                    format_count(
                        aggregate.report_count if aggregate is not None else None
                    ),
                ),
                total,
            )
        )
    return _comparison(tuple(rows), ("Tokens", "Reports"), maximum, console, "Provider")


def _provider_details(
    alias: str | None,
    aggregate: ProviderTokenAggregate | None,
    rollup: ProviderUsageRollup | None,
    console: Console,
) -> list[RenderableType]:
    lines: list[RenderableType] = [label(alias or "unknown provider", console, "bold")]
    if aggregate is not None:
        lines.extend(
            _labeled_values(
                (
                    ("Input", format_count(aggregate.input)),
                    ("Output", format_count(aggregate.output)),
                    ("Total", format_count(aggregate.total)),
                ),
                console,
            )
        )
        lines.extend(
            _labeled_values(
                (
                    ("Cached input", format_count(aggregate.cached_input)),
                    ("Cache write", format_count(aggregate.cache_write)),
                    ("Reasoning", format_count(aggregate.reasoning)),
                ),
                console,
            )
        )
    if rollup is not None:
        lines.extend(
            _labeled_values(
                (
                    ("Usage facts", f"{rollup.terminal_invocations} calls"),
                    ("Recorded attempts", str(rollup.total_attempts)),
                    ("CLI captured", str(rollup.cli_captured_invocations)),
                ),
                console,
            )
        )
        lines.extend(
            _labeled_values(
                (
                    ("Usage full", str(rollup.provider_usage_full_invocations)),
                    ("partial", str(rollup.provider_usage_partial_invocations)),
                    ("malformed", str(rollup.provider_usage_malformed_invocations)),
                    ("Cost", format_cost(rollup.configured_cost_usd)),
                    ("Cost confidence", rollup.configured_cost_confidence),
                ),
                console,
            )
        )
    return lines


def _labeled_values(
    fields: tuple[tuple[str, str], ...], console: Console
) -> list[RenderableType]:
    """Stack measured fields before wrapping can split their exact values."""
    combined = "  ".join(f"{name}: {value}" for name, value in fields)
    if display_width(safe(combined, console)) <= console.width:
        return [text(combined, console)]
    lines: list[RenderableType] = []
    for name, value in fields:
        field = f"{name}: {value}"
        if display_width(safe(field, console)) <= console.width:
            lines.append(text(field, console))
        else:
            lines.extend((wrapped(f"{name}:", console), wrapped(value, console)))
    return lines
