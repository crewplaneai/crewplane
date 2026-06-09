from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from orchestrator_cli.observability.events import (
    InvocationRuntimeState,
    NodeRuntimeState,
)
from orchestrator_cli.observability.node_order import topological_node_order
from orchestrator_cli.observability.types import DashboardSnapshot


@dataclass(frozen=True)
class DashboardSelection:
    ordered_node_ids: list[str]
    selected_index: int
    selected_node_id: str | None


def resolve_dashboard_selection(
    snapshot: DashboardSnapshot,
    selected_index: int,
) -> DashboardSelection:
    ordered_node_ids = topological_node_order(snapshot.state, snapshot.layout)
    if not ordered_node_ids:
        return DashboardSelection(
            ordered_node_ids=ordered_node_ids,
            selected_index=-1,
            selected_node_id=None,
        )
    if selected_index < 0 or selected_index >= len(ordered_node_ids):
        selected_node_id = _default_selected_node_id(
            ordered_node_ids,
            snapshot.state.nodes,
        )
        return DashboardSelection(
            ordered_node_ids=ordered_node_ids,
            selected_index=ordered_node_ids.index(selected_node_id),
            selected_node_id=selected_node_id,
        )
    return DashboardSelection(
        ordered_node_ids=ordered_node_ids,
        selected_index=selected_index,
        selected_node_id=ordered_node_ids[selected_index],
    )


def selected_invocation_log_path(
    nodes: Mapping[str, NodeRuntimeState],
    selected_node_id: str | None,
) -> str | None:
    if selected_node_id is None:
        return None
    invocation = select_invocation(nodes[selected_node_id])
    if invocation is None:
        return None
    return invocation.log_file


def select_invocation(node: NodeRuntimeState) -> InvocationRuntimeState | None:
    if not node.invocations:
        return None

    running = [
        invocation
        for invocation in node.invocations.values()
        if invocation.status == "running"
    ]
    if running:
        return max(running, key=_running_invocation_sort_key)

    non_pending = [
        invocation
        for invocation in node.invocations.values()
        if invocation.status != "pending"
    ]
    if non_pending:
        return max(non_pending, key=_completed_invocation_sort_key)

    return max(node.invocations.values(), key=_completed_invocation_sort_key)


def _default_selected_node_id(
    ordered_node_ids: list[str],
    nodes: Mapping[str, NodeRuntimeState],
) -> str:
    for node_id in ordered_node_ids:
        if nodes[node_id].status == "running":
            return node_id
    return ordered_node_ids[0]


def _running_invocation_sort_key(
    invocation: InvocationRuntimeState,
) -> tuple[float, int, int, str]:
    return (
        _timestamp_sort_value(invocation.started_at),
        _round_num_sort_value(invocation.audit_round_num),
        _round_num_sort_value(invocation.round_num),
        invocation.task_id,
    )


def _completed_invocation_sort_key(
    invocation: InvocationRuntimeState,
) -> tuple[float, float, int, int, str]:
    return (
        _timestamp_sort_value(invocation.finished_at),
        _timestamp_sort_value(invocation.started_at),
        _round_num_sort_value(invocation.audit_round_num),
        _round_num_sort_value(invocation.round_num),
        invocation.task_id,
    )


def _timestamp_sort_value(timestamp: float | None) -> float:
    if timestamp is None:
        return float("-inf")
    return timestamp


def _round_num_sort_value(round_num: int | None) -> int:
    if round_num is None:
        return -1
    return round_num
