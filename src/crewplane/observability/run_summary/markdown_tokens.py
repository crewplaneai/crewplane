"""Render reported-token previews and complete Markdown evidence tables."""

from __future__ import annotations

from .formatting import (
    format_cost,
    format_count,
    full_track,
    node_coverage,
    selected_nodes,
    selected_providers,
)
from .markdown_links import aligned_chart, chart_text, markdown_text, table
from .models import NodeTokenAggregate, ProviderTokenAggregate, RunSummary


def tokens_markdown(summary: RunSummary) -> str:
    """Keep node and provider groupings visible and estimates separate."""
    overall = summary.provider_token_aggregates.overall
    headline = (
        f"Overall reported total: {format_count(overall.total)} across {format_count(overall.report_count)} reports."
        if overall is not None
        else "Overall reported total: n/a (exact totals unavailable)."
    )
    return "\n\n".join(
        (
            headline,
            "### Reported tokens by node\n\n" + _nodes_markdown(summary),
            "### Provider-reported tokens\n\n" + _providers_markdown(summary),
            "Node and provider rows are two views of the same reports; do not add them together. Reported totals exclude unreported usage. Calls, reports and attempts are distinct; full usage status does not establish complete attempt coverage.",
            "### Visible estimates and configured cost\n\n"
            + _provider_estimates_markdown(summary),
        )
    )


def _nodes_markdown(summary: RunSummary) -> str:
    nodes = summary.node_token_aggregates
    selected = selected_nodes(summary)
    maximum = max(
        (node.total for node in nodes if node.total is not None), default=None
    )
    sections = []
    if selected:
        sections.append(
            aligned_chart(
                (
                    "Node",
                    "Relative reported total",
                    "Input",
                    "Output",
                    "Total",
                    "Reports",
                    "Calls w/reports",
                ),
                [
                    (
                        chart_text(node.node_id or ""),
                        full_track(node.total, maximum or 0, 30)
                        if maximum is not None
                        else "",
                        format_count(node.input),
                        format_count(node.output),
                        format_count(node.total),
                        format_count(node.report_count),
                        node_coverage(node),
                    )
                    for node in selected
                ],
            )
        )
    else:
        sections.append("No node usage facts captured.")
    sections.append(
        f"Scale: largest node total = {format_count(maximum)} reported tokens. {len(selected)} shown of {len(nodes)} nodes; {sum(node.total is None for node in nodes)} unknown totals."
    )
    if len(nodes) > len(selected):
        sections.append(
            f"{len(nodes) - len(selected)} more nodes in the complete tables below."
        )
    sections.append(_scale_notice(maximum))
    all_nodes = nodes + (
        (summary.unattributed_node_tokens,)
        if summary.unattributed_node_tokens is not None
        else ()
    )
    if all_nodes:
        sections.extend((_node_buckets(all_nodes), _node_coverage_table(all_nodes)))
    sections.append(
        "Calls w/reports counts observed calls with at least one valid report, not attempt coverage. Incomplete or unknown coverage means reported tokens are not complete consumption. All tasks and review rounds are included; only this run's reports count. Reused results do not copy prior-run usage. Full identities and coverage evidence appear in these complete tables."
    )
    return "\n\n".join(section for section in sections if section)


def _node_label(node: NodeTokenAggregate) -> str:
    return markdown_text(
        node.node_id if node.node_id is not None else "Unattributed (node ID missing)"
    )


def _node_buckets(nodes: tuple[NodeTokenAggregate, ...]) -> str:
    return table(
        (
            "Node",
            "Input",
            "Cached input",
            "Cache write",
            "Output",
            "Reasoning",
            "Total",
            "Reports",
            "Recorded attempts",
        ),
        [
            (
                _node_label(node),
                *(
                    format_count(value)
                    for value in (
                        node.input,
                        node.cached_input,
                        node.cache_write,
                        node.output,
                        node.reasoning,
                        node.total,
                        node.report_count,
                        node.attempt_count,
                    )
                ),
            )
            for node in nodes
        ],
    )


def _node_coverage_table(nodes: tuple[NodeTokenAggregate, ...]) -> str:
    return table(
        (
            "Node",
            "Execution / resume",
            "Calls w/reports",
            "Terminal calls",
            "Positive / zero / missing report-count calls",
            "Missing attempt-count calls",
            "Observed calls without terminal usage",
            "Full / partial / malformed",
            "Parse-error calls",
            "Exact totals",
        ),
        [
            (
                _node_label(node),
                markdown_text(f"{node.status or 'n/a'} / {node.resume_kind or 'n/a'}"),
                node_coverage(node),
                format_count(node.terminal_calls),
                f"{node.positive_report_calls} / {node.zero_report_calls} / {node.missing_report_count_calls}",
                format_count(node.missing_attempt_count_calls),
                format_count(node.calls_without_terminal),
                f"{node.usage_full_calls} / {node.usage_partial_calls} / {node.usage_malformed_calls}",
                format_count(node.parse_error_calls),
                "available" if node.exact else "unavailable",
            )
            for node in nodes
        ],
    )


def _providers_markdown(summary: RunSummary) -> str:
    aggregates = summary.provider_token_aggregates
    by_alias = {item.provider: item for item in aggregates.providers}
    aliases = set(by_alias) | {item.provider for item in summary.provider_rollups}
    maximum = max(
        (item.total for item in aggregates.providers if item.total is not None),
        default=None,
    )
    selected = selected_providers(summary)
    sections = []
    if selected:
        sections.append(
            aligned_chart(
                ("Provider", "Relative reported total", "Tokens", "Reports"),
                [
                    (
                        chart_text(alias or "n/a"),
                        full_track(by_alias[alias].total, maximum or 0, 30)
                        if alias in by_alias and maximum is not None
                        else "",
                        format_count(
                            by_alias[alias].total if alias in by_alias else None
                        ),
                        format_count(
                            by_alias[alias].report_count if alias in by_alias else None
                        ),
                    )
                    for alias in selected
                ],
            )
        )
    sections.append(
        f"Scale: largest provider total = {format_count(maximum)} reported tokens. {len(selected)} shown of {len(aliases)} providers."
    )
    if len(aliases) > len(selected):
        sections.append(
            f"{len(aliases) - len(selected)} more providers in the complete tables below."
        )
    sections.append(_scale_notice(maximum))
    rows = [_provider_row(aggregates.overall)] if aggregates.overall is not None else []
    rows.extend(
        _provider_row(by_alias[alias])
        if alias in by_alias
        else (markdown_text(alias or "n/a"), *("n/a" for _ in range(7)))
        for alias in sorted(aliases, key=lambda alias: alias or "")
    )
    if rows:
        sections.append(
            table(
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
        )
    if summary.provider_rollups:
        sections.append(_provider_rollups_markdown(summary))
    return "\n\n".join(section for section in sections if section)


def _provider_row(aggregate: ProviderTokenAggregate) -> tuple[str, ...]:
    return (
        markdown_text(aggregate.provider or "Overall"),
        *(
            format_count(value)
            for value in (
                aggregate.report_count,
                aggregate.input,
                aggregate.cached_input,
                aggregate.cache_write,
                aggregate.output,
                aggregate.reasoning,
                aggregate.total,
            )
        ),
    )


def _scale_notice(maximum: int | None) -> str:
    if maximum is None:
        return "No usable total measurements were recorded; no total bars are shown."
    return "No positive total was recorded." if maximum == 0 else ""


def _provider_rollups_markdown(summary: RunSummary) -> str:
    if not summary.provider_rollups:
        return "No provider usage rollups captured."
    return table(
        (
            "Provider",
            "Usage calls / recorded attempts",
            "CLI captured",
            "Full / partial / malformed",
        ),
        [
            (
                markdown_text(rollup.provider),
                f"{rollup.terminal_invocations} / {rollup.total_attempts}",
                str(rollup.cli_captured_invocations),
                f"{rollup.provider_usage_full_invocations} / {rollup.provider_usage_partial_invocations} / {rollup.provider_usage_malformed_invocations}",
            )
            for rollup in summary.provider_rollups
        ],
    )


def _provider_estimates_markdown(summary: RunSummary) -> str:
    if not summary.provider_rollups:
        return "No provider usage rollups captured."
    return table(
        ("Provider", "Visible estimate (lower-bound)", "Configured cost / confidence"),
        [
            (
                markdown_text(rollup.provider),
                format_count(rollup.visible_estimate_tokens),
                f"{format_cost(rollup.configured_cost_usd)} / {rollup.configured_cost_confidence}",
            )
            for rollup in summary.provider_rollups
        ],
    )
