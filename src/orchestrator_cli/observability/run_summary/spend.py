from __future__ import annotations

from orchestrator_cli.observability.events import ExecutionEvent
from orchestrator_cli.runtime.agent.usage import (
    InvocationUsage,
    roll_up_cost_confidence,
)

from .formatting import format_cost, format_count
from .models import (
    InvocationUsageSummary,
    ProviderUsageRollup,
    SpendOverviewRow,
    SpendTotals,
    UsageRollupValues,
)


def invocation_usage_summaries(
    events: list[ExecutionEvent],
) -> tuple[InvocationUsageSummary, ...]:
    invocation_events = tuple(
        event
        for event in events
        if event.event_type in {"invocation_finished", "invocation_failed"}
        and event.attempt_count is not None
    )
    return tuple(
        InvocationUsageSummary(
            provider=event.provider or "unknown",
            node_id=event.node_id,
            task_id=event.task_id,
            audit_round_num=event.audit_round_num,
            round_num=event.round_num,
            attempt_count=event.attempt_count or 0,
            cli_captured=bool(event.cli_captured),
            output_extraction_status=event.output_extraction_status or "missing",
            provider_usage_status=event.provider_usage_status or "none",
            provider_tokens=dict(event.provider_tokens or {}),
            visible_estimate_tokens=event.visible_estimate_tokens,
            visible_estimate_method=event.visible_estimate_method,
            visible_estimate_is_lower_bound=bool(event.visible_estimate_is_lower_bound),
            configured_cost_usd=event.configured_cost_usd,
            invocation_cost_confidence=event.invocation_cost_confidence or "none",
            usage_parse_error=event.usage_parse_error,
            failure_kind=event.failure_kind,
            failure_phase=event.failure_phase,
            failure_source=event.failure_source,
            failure_advice=event.failure_advice,
        )
        for event in invocation_events
    )


def spend_totals(
    invocation_usages: tuple[InvocationUsageSummary, ...],
) -> SpendTotals | None:
    if not invocation_usages:
        return None
    values = usage_rollup_values(invocation_usages)
    return SpendTotals(
        terminal_invocations=values.terminal_invocations,
        total_attempts=values.total_attempts,
        cli_captured_invocations=values.cli_captured_invocations,
        provider_usage_full_invocations=values.provider_usage_full_invocations,
        provider_usage_partial_invocations=values.provider_usage_partial_invocations,
        provider_usage_malformed_invocations=values.provider_usage_malformed_invocations,
        visible_estimate_tokens=values.visible_estimate_tokens,
        configured_cost_usd=values.configured_cost_usd,
        configured_cost_confidence=values.configured_cost_confidence,
    )


def usage_rollup_values(
    invocation_usages: tuple[InvocationUsageSummary, ...],
) -> UsageRollupValues:
    return UsageRollupValues(
        terminal_invocations=len(invocation_usages),
        total_attempts=sum(
            invocation_usage.attempt_count for invocation_usage in invocation_usages
        ),
        cli_captured_invocations=sum(
            invocation_usage.cli_captured for invocation_usage in invocation_usages
        ),
        provider_usage_full_invocations=sum(
            invocation_usage.provider_usage_status == "full"
            for invocation_usage in invocation_usages
        ),
        provider_usage_partial_invocations=sum(
            invocation_usage.provider_usage_status == "partial"
            for invocation_usage in invocation_usages
        ),
        provider_usage_malformed_invocations=sum(
            invocation_usage.provider_usage_status == "malformed"
            for invocation_usage in invocation_usages
        ),
        visible_estimate_tokens=sum(
            invocation_usage.visible_estimate_tokens or 0
            for invocation_usage in invocation_usages
        ),
        configured_cost_usd=configured_cost_total(invocation_usages),
        configured_cost_confidence=roll_up_cost_confidence(
            tuple(
                summary_to_usage(invocation_usage)
                for invocation_usage in invocation_usages
            )
        ),
    )


def provider_usage_rollups(
    invocation_usages: tuple[InvocationUsageSummary, ...],
) -> tuple[ProviderUsageRollup, ...]:
    grouped_invocations: dict[str, list[InvocationUsageSummary]] = {}
    for invocation_usage in invocation_usages:
        grouped_invocations.setdefault(invocation_usage.provider, []).append(
            invocation_usage
        )
    rollups: list[ProviderUsageRollup] = []
    for provider in sorted(grouped_invocations):
        provider_invocations = tuple(grouped_invocations[provider])
        values = usage_rollup_values(provider_invocations)
        rollups.append(
            ProviderUsageRollup(
                provider=provider,
                terminal_invocations=values.terminal_invocations,
                total_attempts=values.total_attempts,
                cli_captured_invocations=values.cli_captured_invocations,
                provider_usage_full_invocations=values.provider_usage_full_invocations,
                provider_usage_partial_invocations=values.provider_usage_partial_invocations,
                provider_usage_malformed_invocations=values.provider_usage_malformed_invocations,
                visible_estimate_tokens=values.visible_estimate_tokens,
                configured_cost_usd=values.configured_cost_usd,
                configured_cost_confidence=values.configured_cost_confidence,
            )
        )
    return tuple(rollups)


def configured_cost_total(
    invocation_usages: tuple[InvocationUsageSummary, ...],
) -> float | None:
    if not any(
        invocation_usage.configured_cost_usd is not None
        for invocation_usage in invocation_usages
    ):
        return None
    return sum(
        invocation_usage.configured_cost_usd or 0.0
        for invocation_usage in invocation_usages
    )


def spend_overview_rows(spend: SpendTotals) -> tuple[SpendOverviewRow, ...]:
    return (
        SpendOverviewRow(
            label="Terminal invocations", value=str(spend.terminal_invocations)
        ),
        SpendOverviewRow(
            label="Total attempts",
            value=str(spend.total_attempts),
            show_in_terminal=False,
        ),
        SpendOverviewRow(
            label="CLI invocations captured",
            value=f"{spend.cli_captured_invocations}/{spend.terminal_invocations}",
        ),
        SpendOverviewRow(
            label="Provider token reports",
            value=(
                f"{spend.provider_usage_full_invocations}/"
                f"{spend.terminal_invocations} full, "
                f"{spend.provider_usage_partial_invocations}/"
                f"{spend.terminal_invocations} partial, "
                f"{spend.provider_usage_malformed_invocations}/"
                f"{spend.terminal_invocations} malformed"
            ),
        ),
        SpendOverviewRow(
            label="Visible-text estimate (lower-bound)",
            value=f"{format_count(spend.visible_estimate_tokens)} tokens",
        ),
        SpendOverviewRow(
            label="Configured cost estimate",
            value=format_cost(spend.configured_cost_usd),
            confidence=spend.configured_cost_confidence,
        ),
    )


def summary_to_usage(invocation_usage: InvocationUsageSummary) -> InvocationUsage:
    return InvocationUsage(
        attempt_count=invocation_usage.attempt_count,
        cli_captured=invocation_usage.cli_captured,
        output_extraction_status=invocation_usage.output_extraction_status,
        provider_usage_status=invocation_usage.provider_usage_status,
        provider_tokens=invocation_usage.provider_tokens,
        visible_estimate_tokens=invocation_usage.visible_estimate_tokens,
        visible_estimate_method=invocation_usage.visible_estimate_method,
        visible_estimate_is_lower_bound=invocation_usage.visible_estimate_is_lower_bound,
        configured_cost_usd=invocation_usage.configured_cost_usd,
        invocation_cost_confidence=invocation_usage.invocation_cost_confidence,
        usage_parse_error=invocation_usage.usage_parse_error,
    )
