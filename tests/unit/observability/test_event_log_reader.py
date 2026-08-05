from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import (
    ExecutionEventContext,
    event_from_line,
    event_from_record,
    execution_event_log_record,
    invocation_event,
    read_event_log,
)
from crewplane.observability.run_summary.spend import provider_token_aggregates


def invocation_record(report_count: object = 2) -> dict[str, object]:
    return {
        "event_type": "invocation_finished",
        "workflow_name": "workflow",
        "run_id": "run-1",
        "timestamp": "2026-08-04T00:00:00+00:00",
        "node_id": "node.a",
        "provider": "codex",
        "provider_usage_report_count": report_count,
        "provider_tokens": {
            "input": 10,
            "cached_input": 4,
            "output": 3,
            "reasoning": 1,
            "total": 13,
        },
    }


@pytest.mark.parametrize("report_count", [None, 0, 2])
def test_event_reader_preserves_nullable_and_zero_report_counts(
    report_count: int | None,
) -> None:
    record = invocation_record(report_count)

    event = event_from_record(record)

    assert event is not None
    assert event.payload.provider_usage_report_count == report_count


@pytest.mark.parametrize("invalid_count", [-1, True, "2", 2.5, {}])
def test_event_reader_skips_invalid_report_counts(invalid_count: object) -> None:
    assert event_from_record(invocation_record(invalid_count)) is None


def test_event_reader_accepts_serialized_event_and_legacy_omitted_field() -> None:
    current_event = invocation_event(
        event_type="invocation_finished",
        workflow_name="workflow",
        run_id="run-1",
        context=ExecutionEventContext(
            workflow_name="workflow",
            run_id="run-1",
            node_id="node.a",
            provider="codex",
            role=ProviderRole.EXECUTOR,
            task_id="codex_executor_0",
        ),
        provider_usage_report_count=0,
    )
    current_record = execution_event_log_record(current_event)
    legacy_record = invocation_record()
    legacy_record.pop("provider_usage_report_count")

    assert event_from_record(current_record).payload.provider_usage_report_count == 0
    assert event_from_record(legacy_record).payload.provider_usage_report_count is None


def test_event_reader_keeps_existing_invalid_line_behavior(tmp_path: Path) -> None:
    event_log = tmp_path / "events.ndjson"
    event_log.write_text(
        "\n".join(
            [
                json.dumps(invocation_record()),
                "not json",
                json.dumps({"event_type": "unknown_event"}),
                json.dumps(
                    {
                        "event_type": "runtime_log",
                        "workflow_name": "workflow",
                        "run_id": "run-1",
                        "timestamp": "2026-08-04T00:00:00+00:00",
                        "level": "verbose",
                        "message": "invalid",
                        "operation": "test",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    events = read_event_log(event_log)

    assert len(events) == 1
    assert events[0].payload.provider_usage_report_count == 2


def test_event_reader_skips_blank_and_non_object_lines() -> None:
    assert event_from_line("   ") is None
    assert event_from_line("[]") is None


@pytest.mark.parametrize(
    "missing_field", ["event_type", "workflow_name", "run_id", "timestamp"]
)
def test_event_reader_requires_event_identity_fields(missing_field: str) -> None:
    record = invocation_record()
    record.pop(missing_field)

    assert event_from_record(record) is None


def test_event_reader_preserves_invalid_timestamp_as_epoch() -> None:
    event = event_from_record({**invocation_record(), "timestamp": "not-a-timestamp"})

    assert event is not None
    assert event.timestamp == 0.0


def test_event_reader_rejects_invalid_token_and_log_attribute_values() -> None:
    invalid_tokens = {
        **invocation_record(),
        "provider_tokens": {"input": True},
    }
    invalid_attributes = {
        "event_type": "runtime_log",
        "workflow_name": "workflow",
        "run_id": "run-1",
        "timestamp": "2026-08-04T00:00:00+00:00",
        "level": "info",
        "message": "message",
        "operation": "operation",
        "attributes": {"nested": []},
    }

    assert event_from_record(invalid_tokens) is not None
    assert event_from_record(invalid_tokens).payload.provider_tokens is None
    assert event_from_record(invalid_attributes) is not None
    assert event_from_record(invalid_attributes).payload.attributes is None


def test_event_reader_returns_empty_for_missing_or_symlinked_logs(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing.ndjson"
    source_path = tmp_path / "source.ndjson"
    link_path = tmp_path / "link.ndjson"
    source_path.write_text(json.dumps(invocation_record()), encoding="utf-8")
    try:
        link_path.symlink_to(source_path)
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert read_event_log(missing_path) == []
    assert read_event_log(link_path) == []


def test_legacy_provider_tokens_remain_readable_but_are_excluded_from_exact_totals() -> (
    None
):
    legacy_event = event_from_line(json.dumps(invocation_record(None)))

    assert legacy_event is not None
    aggregates = provider_token_aggregates([legacy_event])

    assert aggregates.overall is None
    assert dict(legacy_event.payload.provider_tokens)["input"] == 10
