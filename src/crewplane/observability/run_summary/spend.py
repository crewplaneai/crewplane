from __future__ import annotations

from dataclasses import dataclass, field

from crewplane.architecture.contracts import (
    AggregateCostConfidence,
    InvocationCostConfidence,
)
from crewplane.observability.events import ExecutionEvent, InvocationEventPayload

from .formatting import format_cost, format_count
from .models import (
    InvocationUsageSummary,
    ProviderUsageRollup,
    SpendOverviewRow,
    SpendTotals,
    UsageRollupValues,
)


@dataclass
class _MutableUsageRollup:
    terminal_invocations: int = 0
    total_attempts: int = 0
    cli_captured_invocations: int = 0
    provider_usage_full_invocations: int = 0
    provider_usage_partial_invocations: int = 0
    provider_usage_malformed_invocations: int = 0
    visible_estimate_tokens: int = 0
    configured_cost_usd: float | None = None
    cost_confidences: set[InvocationCostConfidence] = field(default_factory=set)

    def record(self, invocation_usage: InvocationUsageSummary) -> None:
        self.terminal_invocations += 1
        self.total_attempts += invocation_usage.attempt_count
        self.cli_captured_invocations += int(invocation_usage.cli_captured)
        self.provider_usage_full_invocations += int(
            invocation_usage.provider_usage_status == "full"
        )
        self.provider_usage_partial_invocations += int(
            invocation_usage.provider_usage_status == "partial"
        )
        self.provider_usage_malformed_invocations += int(
            invocation_usage.provider_usage_status == "malformed"
        )
        self.visible_estimate_tokens += invocation_usage.visible_estimate_tokens or 0
        if invocation_usage.configured_cost_usd is not None:
            if self.configured_cost_usd is None:
                self.configured_cost_usd = 0.0
            self.configured_cost_usd += invocation_usage.configured_cost_usd
        self.cost_confidences.add(invocation_usage.invocation_cost_confidence)

    def values(self) -> UsageRollupValues:
        return UsageRollupValues(
            terminal_invocations=self.terminal_invocations,
            total_attempts=self.total_attempts,
            cli_captured_invocations=self.cli_captured_invocations,
            provider_usage_full_invocations=self.provider_usage_full_invocations,
            provider_usage_partial_invocations=self.provider_usage_partial_invocations,
            provider_usage_malformed_invocations=(
                self.provider_usage_malformed_invocations
            ),
            visible_estimate_tokens=self.visible_estimate_tokens,
            configured_cost_usd=self.configured_cost_usd,
            configured_cost_confidence=aggregate_cost_confidence(self.cost_confidences),
        )


class UsageRollupAccumulator:
    """Streaming usage rollups that do not retain every invocation detail."""

    def __init__(self) -> None:
        self._overall = _MutableUsageRollup()
        self._providers: dict[str, _MutableUsageRollup] = {}

    def record(self, invocation_usage: InvocationUsageSummary) -> None:
        self._overall.record(invocation_usage)
        self._providers.setdefault(
            invocation_usage.provider,
            _MutableUsageRollup(),
        ).record(invocation_usage)

    def spend_totals(self) -> SpendTotals | None:
        if self._overall.terminal_invocations == 0:
            return None
        return spend_totals_from_values(self._overall.values())

    def provider_usage_rollups(self) -> tuple[ProviderUsageRollup, ...]:
        rollups: list[ProviderUsageRollup] = []
        for provider in sorted(self._providers):
            values = self._providers[provider].values()
            rollups.append(
                ProviderUsageRollup(
                    provider=provider,
                    terminal_invocations=values.terminal_invocations,
                    total_attempts=values.total_attempts,
                    cli_captured_invocations=values.cli_captured_invocations,
                    provider_usage_full_invocations=(
                        values.provider_usage_full_invocations
                    ),
                    provider_usage_partial_invocations=(
                        values.provider_usage_partial_invocations
                    ),
                    provider_usage_malformed_invocations=(
                        values.provider_usage_malformed_invocations
                    ),
                    visible_estimate_tokens=values.visible_estimate_tokens,
                    configured_cost_usd=values.configured_cost_usd,
                    configured_cost_confidence=values.configured_cost_confidence,
                )
            )
        return tuple(rollups)


def invocation_usage_summaries(
    events: list[ExecutionEvent],
) -> tuple[InvocationUsageSummary, ...]:
    summaries: list[InvocationUsageSummary] = []
    for event in events:
        summary = invocation_usage_summary_from_event(event)
        if summary is None:
            continue
        summaries.append(summary)
    return tuple(summaries)


def invocation_usage_summary_from_event(
    event: ExecutionEvent,
) -> InvocationUsageSummary | None:
    if event.event_type not in {"invocation_finished", "invocation_failed"}:
        return None
    payload = invocation_payload(event)
    if payload.attempt_count is None:
        return None
    context = event.context
    return InvocationUsageSummary(
        provider=context.provider or "unknown",
        node_id=context.node_id,
        task_id=context.task_id,
        audit_round_num=context.audit_round_num,
        round_num=context.round_num,
        attempt_count=payload.attempt_count,
        cli_captured=bool(payload.cli_captured),
        output_extraction_status=payload.output_extraction_status or "missing",
        provider_usage_status=payload.provider_usage_status or "none",
        provider_tokens=dict(payload.provider_tokens or {}),
        visible_estimate_tokens=payload.visible_estimate_tokens,
        visible_estimate_method=payload.visible_estimate_method,
        visible_estimate_is_lower_bound=bool(payload.visible_estimate_is_lower_bound),
        configured_cost_usd=payload.configured_cost_usd,
        invocation_cost_confidence=payload.invocation_cost_confidence or "none",
        usage_parse_error=payload.usage_parse_error,
        failure_kind=payload.failure_kind,
        failure_phase=payload.failure_phase,
        failure_source=payload.failure_source,
        failure_advice=payload.failure_advice,
    )


def invocation_payload(event: ExecutionEvent) -> InvocationEventPayload:
    if not isinstance(event.payload, InvocationEventPayload):
        raise TypeError(
            f"Expected invocation payload for event_type '{event.event_type}'."
        )
    return event.payload


def spend_totals(
    invocation_usages: tuple[InvocationUsageSummary, ...],
) -> SpendTotals | None:
    if not invocation_usages:
        return None
    values = usage_rollup_values(invocation_usages)
    return spend_totals_from_values(values)


def spend_totals_from_values(values: UsageRollupValues) -> SpendTotals:
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
    rollup = _MutableUsageRollup()
    for invocation_usage in invocation_usages:
        rollup.record(invocation_usage)
    return rollup.values()


def provider_usage_rollups(
    invocation_usages: tuple[InvocationUsageSummary, ...],
) -> tuple[ProviderUsageRollup, ...]:
    accumulator = UsageRollupAccumulator()
    for invocation_usage in invocation_usages:
        accumulator.record(invocation_usage)
    return accumulator.provider_usage_rollups()


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


def roll_up_cost_confidence(
    invocation_usages: tuple[InvocationUsageSummary, ...],
) -> AggregateCostConfidence:
    return aggregate_cost_confidence(
        {
            invocation_usage.invocation_cost_confidence
            for invocation_usage in invocation_usages
        }
    )


def aggregate_cost_confidence(
    confidences: set[InvocationCostConfidence],
) -> AggregateCostConfidence:
    if not confidences:
        return "none"
    if confidences == {"full"}:
        return "full"
    if confidences == {"none"}:
        return "none"
    if confidences <= {"full", "partial"} and "partial" in confidences:
        return "partial"
    return "mixed"
