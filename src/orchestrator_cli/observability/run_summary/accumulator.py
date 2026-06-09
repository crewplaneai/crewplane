from __future__ import annotations

from collections import deque

from orchestrator_cli.observability.events import (
    ExecutionEvent,
    RuntimeLogEventPayload,
)

from .models import InvocationUsageSummary, RunSummaryFacts
from .spend import UsageRollupAccumulator, invocation_usage_summary_from_event

MAX_RETAINED_INVOCATION_USAGE_DETAILS = 200


class RunSummaryAccumulator:
    """Streaming aggregate facts independent of retained event details."""

    def __init__(self) -> None:
        self._invocation_usages: deque[InvocationUsageSummary] = deque(
            maxlen=MAX_RETAINED_INVOCATION_USAGE_DETAILS
        )
        self._omitted_invocation_usage_count = 0
        self._usage_rollups = UsageRollupAccumulator()
        self._review_consensus_unresolved = False
        self._started_at = "n/a"
        self._completed_at = "n/a"

    def record(self, event: ExecutionEvent) -> None:
        if event.event_type == "workflow_started":
            self._started_at = event.timestamp_utc
        if event.event_type in {"workflow_finished", "workflow_failed"}:
            self._completed_at = event.timestamp_utc
        usage_summary = invocation_usage_summary_from_event(event)
        if usage_summary is not None:
            self._usage_rollups.record(usage_summary)
            if len(self._invocation_usages) == MAX_RETAINED_INVOCATION_USAGE_DETAILS:
                self._omitted_invocation_usage_count += 1
            self._invocation_usages.append(usage_summary)
        if _is_unresolved_consensus_event(event):
            self._review_consensus_unresolved = True

    def snapshot(self) -> RunSummaryFacts:
        return RunSummaryFacts(
            invocation_usages=tuple(self._invocation_usages),
            spend=self._usage_rollups.spend_totals(),
            provider_rollups=self._usage_rollups.provider_usage_rollups(),
            omitted_invocation_usage_count=self._omitted_invocation_usage_count,
            review_consensus_unresolved=self._review_consensus_unresolved,
            started_at=self._started_at,
            completed_at=self._completed_at,
        )


def _is_unresolved_consensus_event(event: ExecutionEvent) -> bool:
    return (
        event.event_type == "runtime_log"
        and isinstance(event.payload, RuntimeLogEventPayload)
        and event.payload.operation == "review_loop_consensus_exhausted"
        and event.payload.attributes is not None
        and event.payload.attributes.get("continued") is True
    )
