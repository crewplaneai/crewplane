from __future__ import annotations

from typing import Any

import pytest

from crewplane.architecture.contracts import TopologyNode, WorkflowTopology
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability import timing
from crewplane.observability.events import (
    InvocationRuntimeState,
    NodeRuntimeState,
    build_initial_state,
)
from crewplane.observability.layout import compute_topology_layout
from crewplane.observability.render import RenderConfig, render_dashboard_text
from crewplane.observability.render.cells import node_cell_lines
from crewplane.observability.timing import ElapsedTimer
from crewplane.observability.tmux.selected_invocation import prepare_selected_invocation
from crewplane.observability.tmux.selection import (
    resolve_dashboard_selection,
    selected_invocation_log_path,
)
from crewplane.observability.types import DashboardSnapshot


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("min_lane_width", 0, "min_lane_width must be greater than 0"),
        ("divider", None, "divider must be a string"),
        ("rotate_interval_seconds", float("nan"), "must be finite"),
        ("rotate_interval_seconds", float("inf"), "must be finite"),
        ("rotate_interval_seconds", 0, "must be greater than 0"),
        ("stream_lines_per_node", -1, "must be greater than or equal to 0"),
        ("display_mode", "unknown", "must be 'waves' or 'timeline'"),
    ],
)
def test_render_config_rejects_invalid_values(
    field: str, value: Any, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        RenderConfig(**{field: value})


@pytest.mark.parametrize(
    ("statuses", "summary"),
    [
        (["running"], "RUN task-0"),
        (["running", "running"], "RUN task-0 (+1)"),
        (["succeeded", "failed"], "FAIL 1/2"),
        (["succeeded", "succeeded"], "DONE 2/2"),
    ],
)
@pytest.mark.parametrize(
    ("left", "right", "title"),
    [(False, False, "node [parallel]"), (True, True, "< node [parallel] >")],
)
def test_node_cell_displays_invocation_summary_and_clipping(
    statuses: list[Any], summary: str, left: bool, right: bool, title: str
) -> None:
    node = NodeRuntimeState("node", "parallel", ("mock",))
    node.invocations = {
        f"task-{index}": InvocationRuntimeState(
            f"task-{index}", "mock", ProviderRole.EXECUTOR, None, None, None, status
        )
        for index, status in enumerate(statuses)
    }

    lines = node_cell_lines(node, 40, left, right, 0, [])

    assert [line.rstrip() for line in lines] == [title, summary, "state=pending"]


def test_timer_rejects_stop_before_start() -> None:
    timer = ElapsedTimer()

    with pytest.raises(RuntimeError, match="has not been started"):
        timer.stop()


@pytest.mark.parametrize("operation", ["elapsed_seconds", "elapsed_milliseconds"])
def test_timer_rejects_reads_before_start(operation: str) -> None:
    timer = ElapsedTimer()

    with pytest.raises(RuntimeError, match="has not been started"):
        getattr(timer, operation)


def test_timer_reports_live_elapsed_and_keeps_stopped_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readings = iter([10.0, 10.25, 10.5])
    monkeypatch.setattr(timing, "monotonic", readings.__next__)

    with ElapsedTimer() as timer:
        assert timer.elapsed_milliseconds == 250
        assert timer.stop() == 0.5
        assert timer.stop() == 0.5

    assert timer.elapsed_seconds == 0.5
    assert timer.elapsed_milliseconds == 500


def test_empty_dashboard_renders_and_has_no_selection() -> None:
    topology = WorkflowTopology("empty", ())
    state = build_initial_state(topology, "run")
    layout = compute_topology_layout(topology)
    snapshot = DashboardSnapshot(state, layout, 0.0)

    rendered = render_dashboard_text(state, layout, 60, 10, now=0.0)
    selection = resolve_dashboard_selection(snapshot, -1)

    assert "empty" in rendered
    assert selection.ordered_node_ids == []
    assert selection.selected_node_id is None
    assert selection.selected_index == -1
    assert prepare_selected_invocation(state.nodes, None, 10, None, 0.0) is None
    assert selected_invocation_log_path(state.nodes, None) is None


def test_finished_dashboard_focuses_final_nodes() -> None:
    topology = WorkflowTopology(
        "flow",
        tuple(
            TopologyNode(f"node{i}", "parallel", (f"node{i - 1}",) if i else ())
            for i in range(5)
        ),
    )
    state = build_initial_state(topology, "run")
    layout = compute_topology_layout(topology)
    for node in state.nodes.values():
        node.status = "succeeded"

    rendered = render_dashboard_text(state, layout, 80, 13, now=0.0)

    assert "node4 [parallel]" in rendered
    assert "node0 [parallel]" not in rendered


@pytest.mark.parametrize("has_invocation", [False, True])
def test_selected_log_path_handles_pending_invocations(has_invocation: bool) -> None:
    node = NodeRuntimeState("node", "parallel", ("mock",))
    if has_invocation:
        node.invocations["task"] = InvocationRuntimeState(
            "task",
            "mock",
            ProviderRole.EXECUTOR,
            None,
            None,
            None,
            log_file="provider.log",
        )

    assert selected_invocation_log_path({"node": node}, "node") == (
        "provider.log" if has_invocation else None
    )
