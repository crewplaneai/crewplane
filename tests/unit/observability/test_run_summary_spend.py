from __future__ import annotations

import pytest

from crewplane.architecture.contracts import ProviderTokenUsage
from crewplane.architecture.contracts.invocation import TOKEN_BUCKETS
from crewplane.observability.events import event_from_record
from crewplane.observability.run_summary.spend import (
    UsageRollupAccumulator,
    aggregate_cost_confidence,
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


@pytest.mark.parametrize("bucket", TOKEN_BUCKETS)
@pytest.mark.parametrize("value", [None, 0, 7])
def test_every_serialized_token_bucket_reaches_aggregation(bucket, value) -> None:
    usage = ProviderTokenUsage(**{bucket: value})
    events = [
        event_from_record(invocation_record(tokens=usage.as_dict())) for _ in range(2)
    ]
    assert all(event is not None for event in events)
    aggregate = provider_token_aggregates(events)
    assert aggregate.overall is not None
    assert aggregate.overall.report_count == 2
    for result in (aggregate.overall, *aggregate.providers):
        assert getattr(result, bucket) == (None if value is None else 2 * value)
        assert all(
            getattr(result, other) is None for other in TOKEN_BUCKETS if other != bucket
        )


@pytest.mark.parametrize("bucket", TOKEN_BUCKETS)
@pytest.mark.parametrize(
    "values, expected",
    [
        ([5, None], None),
        ([None, 5], None),
        ([0, 5], 5),
        ([5, 0], 5),
        ([-1, 5], None),
        ([5, -1], None),
        ([True, 5], None),
        ([5, "7"], None),
    ],
)
def test_token_aggregation_preserves_unknown_and_invalid_contributions(
    bucket: str, values: list[object], expected: int | None
) -> None:
    events = []
    for value in values:
        record = invocation_record()
        record["provider_tokens"] = {bucket: value}
        event = event_from_record(record)
        assert event is not None
        events.append(event)
    aggregates = provider_token_aggregates(events)
    assert aggregates.overall is not None
    for result in (aggregates.overall, *aggregates.providers):
        assert result.report_count == 2
        assert getattr(result, bucket) == expected


def test_all_unknown_positive_report_poisoning_survives_zero_report_rows() -> None:
    events = [
        event_from_record(invocation_record(report_count=0, tokens={"input": 99})),
        event_from_record(invocation_record(report_count=2, tokens={})),
        event_from_record(
            invocation_record(tokens={bucket: 0 for bucket in TOKEN_BUCKETS})
        ),
    ]
    assert all(event is not None for event in events)
    aggregates = provider_token_aggregates(events)
    assert aggregates.overall is not None
    for result in (aggregates.overall, *aggregates.providers):
        assert result.report_count == 3
        assert all(getattr(result, bucket) is None for bucket in TOKEN_BUCKETS)


@pytest.mark.parametrize(
    ("confidences", "expected"),
    [
        ((), "none"),
        (("none",), "none"),
        (("full",), "full"),
        (("partial",), "partial"),
        (("full", "partial"), "partial"),
        (("full", "none"), "mixed"),
        (("partial", "none"), "mixed"),
        (("full", "partial", "none"), "mixed"),
    ],
)
@pytest.mark.parametrize("cost", [None, 0.0, 0.25])
def test_cost_confidence_subsets_reach_streaming_and_batch_totals(
    confidences, expected, cost
) -> None:
    assert aggregate_cost_confidence(set(confidences)) == expected
    events = []
    for confidence in confidences * 2:
        record = invocation_record()
        record.update(invocation_cost_confidence=confidence, configured_cost_usd=cost)
        event = event_from_record(record)
        assert event is not None
        events.append(event)
    summaries = invocation_usage_summaries(events)
    accumulator = UsageRollupAccumulator()
    for summary in summaries:
        accumulator.record(summary)
    total = spend_totals(summaries)
    assert accumulator.spend_totals() == total
    assert accumulator.provider_usage_rollups() == provider_usage_rollups(summaries)
    if not confidences:
        assert total is None
        return
    assert total is not None
    assert total.configured_cost_confidence == expected
    assert total.configured_cost_usd == (None if cost is None else cost * len(events))
