from __future__ import annotations

from crewplane.observability.events import event_from_record
from crewplane.observability.run_summary.spend import (
    invocation_usage_summaries,
    provider_token_aggregates,
    provider_usage_rollups,
    spend_totals,
)


def invocation_record(
    event_type: str = "invocation_finished",
    provider: str | None = "codex",
    report_count: int | None = 1,
    attempt_count: int | None = 1,
    tokens: dict[str, int | None] | None = None,
) -> dict[str, object]:
    return {
        "event_type": event_type,
        "workflow_name": "workflow",
        "run_id": "run-1",
        "timestamp": "2026-08-04T00:00:00+00:00",
        "node_id": "node.a",
        "provider": provider,
        "attempt_count": attempt_count,
        "provider_usage_report_count": report_count,
        "provider_tokens": tokens
        if tokens is not None
        else {"input": 10, "output": 3, "total": 13},
    }


def test_provider_token_aggregates_sum_terminal_reports_and_preserve_unknown_buckets() -> (
    None
):
    events = [
        event_from_record(invocation_record()),
        event_from_record(
            invocation_record(
                event_type="invocation_failed",
                provider="claude",
                tokens={"input": 5, "output": 7},
            )
        ),
        event_from_record(
            invocation_record(
                provider="unknown-provider",
                report_count=0,
                tokens={"input": 999, "output": 999},
            )
        ),
        event_from_record(invocation_record(event_type="invocation_started")),
        event_from_record(invocation_record(report_count=None)),
    ]
    valid_events = [event for event in events if event is not None]

    aggregates = provider_token_aggregates(valid_events)

    assert aggregates.overall is not None
    assert aggregates.overall.report_count == 2
    assert aggregates.overall.input == 15
    assert aggregates.overall.output == 10
    assert aggregates.overall.total is None
    assert [(item.provider, item.report_count) for item in aggregates.providers] == [
        ("claude", 1),
        ("codex", 1),
        ("unknown-provider", 0),
    ]


def test_spend_helpers_filter_nonterminal_and_missing_attempt_events() -> None:
    valid = event_from_record(invocation_record())
    missing_attempt = event_from_record(invocation_record(attempt_count=None))
    started = event_from_record(invocation_record(event_type="invocation_started"))
    assert valid is not None
    assert missing_attempt is not None
    assert started is not None

    summaries = invocation_usage_summaries([valid, missing_attempt, started])

    assert len(summaries) == 1
    assert spend_totals(summaries) is not None
    assert spend_totals(()) is None
    assert provider_usage_rollups(summaries)[0].provider == "codex"
