from dataclasses import replace

import pytest

from crewplane.architecture.contracts import EventType
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import (
    ExecutionEvent,
    InvocationRuntimeState,
    NodeRuntimeState,
    RunDashboardState,
    apply_event,
    event_from_record,
    execution_event_log_record,
)
from crewplane.observability.layout import TopologyLayout
from crewplane.observability.run_summary.builder import invocation_timing_summaries
from crewplane.observability.run_summary.token_aggregation import aggregate_tokens
from crewplane.observability.types import DashboardSnapshot
from tests.helpers.observability import make_execution_event


def usage_event(
    node_id: str | None = "node", task_id: str | None = "task", **fields
) -> ExecutionEvent:
    event = make_execution_event(
        event_type=fields.pop("event_type", EventType.INVOCATION_FINISHED),
        workflow_name="flow",
        run_id="run",
        node_id=node_id or "node",
        task_id=task_id or "task",
        provider=fields.pop("provider", "shared"),
        role=ProviderRole.EXECUTOR,
        attempt_count=fields.pop("attempt_count", 1),
        provider_usage_report_count=fields.pop("provider_usage_report_count", 1),
        provider_tokens=fields.pop(
            "provider_tokens", {"input": 7, "output": 3, "total": 10}
        ),
        **fields,
    )

    return replace(
        event, context=replace(event.context, node_id=node_id, task_id=task_id)
    )


def snapshot_with_calls(*events: ExecutionEvent) -> DashboardSnapshot:
    nodes: dict[str, NodeRuntimeState] = {}
    for index, event in enumerate(events):
        context = event.context
        assert context.node_id and context.task_id
        node = nodes.setdefault(
            context.node_id, NodeRuntimeState(context.node_id, "parallel", ())
        )
        node.invocations[str(index)] = InvocationRuntimeState(
            context.task_id,
            "shared",
            ProviderRole.EXECUTOR,
            None,
            context.audit_round_num,
            context.round_num,
        )
    order = {key: index for index, key in enumerate(nodes)}
    return DashboardSnapshot(
        RunDashboardState("flow", "run", order, nodes=nodes),
        TopologyLayout((), {}, 0, order, {}, {}),
        0,
    )


def test_node_scopes_reconcile_and_keep_complete_identity() -> None:
    events = [
        usage_event("group.child[0]", audit_round_num=1, round_num=1),
        usage_event(
            "group.child[0]",
            audit_round_num=1,
            round_num=2,
            provider="other",
            provider_usage_report_count=3,
            attempt_count=4,
        ),
        usage_event("other", event_type=EventType.INVOCATION_FAILED),
    ]
    result = aggregate_tokens(events, snapshot_with_calls(*events))
    assert result.provider.overall is not None
    assert result.provider.overall.total == 30
    assert sum(row.total for row in result.nodes) == 30
    assert sum(row.report_count for row in result.nodes) == 5
    first, second = result.nodes
    assert (
        first.node_id,
        first.total,
        first.calls_with_reports,
        first.observed_calls,
    ) == ("group.child[0]", 20, 2, 2)
    assert first.attempt_count == 5
    assert second.calls_with_reports == 1


def test_cancellation_without_terminal_usage_retains_reported_subset() -> None:
    finished = usage_event()
    started = usage_event(task_id="cancelled", event_type=EventType.INVOCATION_STARTED)
    result = aggregate_tokens(
        [finished, started], snapshot_with_calls(finished, started)
    ).nodes[0]
    assert (
        result.total,
        result.observed_calls,
        result.calls_with_reports,
        result.calls_without_terminal,
    ) == (10, 2, 1, 1)


@pytest.mark.parametrize("report_count", [None, -1, True, "1"])
def test_invalid_report_counts_keep_coverage_without_tokens(report_count) -> None:
    valid = usage_event()
    missing = usage_event(task_id="missing", provider_usage_report_count=report_count)
    result = aggregate_tokens([valid, missing], snapshot_with_calls(valid, missing))
    row = result.nodes[0]
    assert (row.total, row.report_count, row.missing_report_count_calls) == (10, 1, 1)
    assert (row.calls_with_reports, row.observed_calls) == (1, 2)
    assert result.provider.overall is not None and result.provider.overall.total == 10


def test_missing_attempts_do_not_discard_valid_reports_or_usage_evidence() -> None:
    event = usage_event(
        attempt_count=None,
        provider_usage_status="full",
        usage_parse_error="bad fragment",
    )
    row = aggregate_tokens([event]).nodes[0]
    assert (row.total, row.report_count, row.attempt_count) == (10, 1, None)
    assert (
        row.missing_attempt_count_calls,
        row.usage_full_calls,
        row.parse_error_calls,
    ) == (1, 1, 1)
    assert row.observed_calls is None


def test_unattributed_is_separate_from_literal_display_label_node() -> None:
    label = "Unattributed (node ID missing)"
    result = aggregate_tokens(
        [usage_event(None), usage_event("  "), usage_event(label)]
    )
    assert len(result.nodes) == 1 and result.nodes[0].node_id == label
    assert result.unattributed is not None and result.unattributed.total == 20
    assert result.provider.overall is not None and result.provider.overall.total == 30


@pytest.mark.parametrize("missing", ["node", "task", "snapshot", "duplicate"])
def test_inconsistent_identities_never_invent_call_coverage(missing: str) -> None:
    event = usage_event()
    snapshot = snapshot_with_calls(event)
    if missing == "node":
        event = replace(event, context=replace(event.context, node_id="disk-only"))
    if missing == "task":
        event = replace(event, context=replace(event.context, task_id=None))
    result = aggregate_tokens(
        [event] * (2 if missing == "duplicate" else 1),
        None if missing == "snapshot" else snapshot,
    )
    row = next(row for row in result.nodes if row.total is not None)
    assert row.total == (20 if missing == "duplicate" else 10)
    assert row.observed_calls is None and row.calls_with_reports is None


def test_zero_report_and_no_new_calls_are_not_measured_zero() -> None:
    event = usage_event(provider_usage_report_count=0)
    snapshot = snapshot_with_calls(event)
    snapshot.state.nodes["group"] = NodeRuntimeState("group", "group", ())
    result = aggregate_tokens([event], snapshot)
    group, node = result.nodes
    assert (group.report_count, group.total, group.observed_calls) == (None, None, 0)
    assert (node.report_count, node.total, node.zero_report_calls) == (0, None, 1)
    measured = aggregate_tokens([usage_event(provider_tokens={"total": 0})]).nodes[0]
    assert measured.total == 0


def test_unknown_bucket_poisoning_matches_both_projections() -> None:
    result = aggregate_tokens(
        [usage_event(), usage_event(task_id="other", provider_tokens={"input": 3})]
    )
    assert result.provider.overall is not None
    assert result.nodes[0].total is None and result.provider.overall.total is None
    assert result.nodes[0].input == result.provider.overall.input == 10


@pytest.mark.parametrize(
    "values, expected",
    [(("low", "max", None), "max"), ((None, "low"), "low"), ((None,), None)],
)
def test_requested_reasoning_survives_later_updates_missing_and_replay(
    values, expected
) -> None:
    state = RunDashboardState(
        "flow",
        "run",
        {"node": 0},
        nodes={"node": NodeRuntimeState("node", "parallel", ())},
    )
    for reasoning in values:
        event = usage_event(requested_reasoning=reasoning)
        replayed = event_from_record(execution_event_log_record(event))
        assert replayed is not None
        apply_event(state, replayed)
    invocation = next(iter(state.nodes["node"].invocations.values()))
    assert invocation.requested_reasoning == expected
    snapshot = DashboardSnapshot(
        state, TopologyLayout((), {}, 0, {"node": 0}, {}, {}), 0
    )
    assert invocation_timing_summaries(snapshot)[0].requested_reasoning == expected


def test_decoder_rejection_leaves_snapshot_call_uncovered() -> None:
    event = usage_event()
    record = execution_event_log_record(event)
    record["provider_usage_report_count"] = "malformed"
    assert event_from_record(record) is None
    row = aggregate_tokens([], snapshot_with_calls(event)).nodes[0]
    assert row.total is None
    assert (row.calls_with_reports, row.observed_calls, row.calls_without_terminal) == (
        0,
        1,
        1,
    )
