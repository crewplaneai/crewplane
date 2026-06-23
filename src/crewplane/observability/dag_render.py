from __future__ import annotations

from dataclasses import dataclass

from crewplane.observability.dag_graph import (
    build_graph_states,
    column_budget,
    graph_width,
    render_connector_graph,
    render_node_graph,
)
from crewplane.observability.events import NodeRuntimeState, RunDashboardState
from crewplane.observability.layout import TopologyLayout
from crewplane.observability.node_order import topological_node_order
from crewplane.observability.status_icons import status_icon
from crewplane.observability.text_layout import (
    display_width,
    fit_text,
    pad_text,
)
from crewplane.observability.timing import format_elapsed_seconds

MAX_GRAPH_WIDTH = 12


@dataclass(frozen=True)
class DagRenderConfig:
    """Configuration for compact DAG summary rendering."""

    max_graph_width: int = MAX_GRAPH_WIDTH


def render_dag_summary(
    state: RunDashboardState,
    layout: TopologyLayout,
    selected_node_id: str | None,
    width: int,
    now: float | None = None,
    config: DagRenderConfig = DagRenderConfig(),
) -> list[str]:
    """Render a compact, line-oriented DAG summary for live dashboards."""

    ordered_node_ids = topological_node_order(state, layout)
    if not ordered_node_ids:
        return [fit_text("No workflow nodes.", width)]

    graph_states = build_graph_states(ordered_node_ids, layout)
    max_columns = max(
        (graph_state.column_count for graph_state in graph_states),
        default=1,
    )
    visible_columns = min(max(1, max_columns), column_budget(config.max_graph_width))
    hidden_columns = max(0, max_columns - visible_columns)
    graph_column_width = graph_width(visible_columns)

    lines: list[str] = []
    for index, graph_state in enumerate(graph_states):
        node = state.nodes[graph_state.node_id]
        graph = render_node_graph(
            graph_state=graph_state,
            visible_columns=visible_columns,
        )
        marker = "▸" if graph_state.node_id == selected_node_id else " "
        graph_column = pad_text(graph, graph_column_width)
        node_column_width = max(24, display_width(graph_state.node_id))
        node_column = pad_text(graph_state.node_id, node_column_width)
        line = f"{marker}{graph_column} {node_column} {_node_meta(node, now)}"
        lines.append(fit_text(line.rstrip(), width))

        next_node_id = (
            graph_states[index + 1].node_id if index + 1 < len(graph_states) else None
        )
        connector = render_connector_graph(
            graph_state=graph_state,
            next_node_id=next_node_id,
            visible_columns=visible_columns,
        )
        if connector is not None:
            lines.append(fit_text(f" {connector}", width))

    if hidden_columns:
        lines.append(fit_text(f"... +{hidden_columns} more", width))
    return lines


def _node_meta(node: NodeRuntimeState, now: float | None) -> str:
    icon = status_icon(node.status)
    elapsed = _node_elapsed_label(node, now)
    if node.status == "failed":
        return f"{icon} {elapsed} {_node_failure_note(node)}".strip()
    if node.status == "blocked":
        return f"{icon} {elapsed} blocked".strip()

    provider_chain = _node_provider_chain(node)
    return f"{icon} {elapsed} {provider_chain}".strip()


def _node_elapsed_label(node: NodeRuntimeState, now: float | None) -> str:
    elapsed_seconds = _node_elapsed_seconds(node, now)
    if elapsed_seconds is None:
        return ""
    return format_elapsed_seconds(elapsed_seconds)


def _node_elapsed_seconds(
    node: NodeRuntimeState,
    now: float | None,
) -> float | None:
    if node.started_at is not None:
        if node.finished_at is not None:
            return max(0.0, node.finished_at - node.started_at)
        if node.status == "running" and now is not None:
            return max(0.0, now - node.started_at)
        return None

    if node.status == "running":
        return None

    duration_ms = max(
        (invocation.duration_ms or 0 for invocation in node.invocations.values()),
        default=0,
    )
    if duration_ms <= 0:
        return None
    return duration_ms / 1000


def _node_failure_note(node: NodeRuntimeState) -> str:
    if not node.recent_events:
        return "failed"
    latest = node.recent_events[-1]
    if latest.startswith("FAIL "):
        return latest[len("FAIL ") :]
    return latest


def _node_provider_chain(node: NodeRuntimeState) -> str:
    if not node.configured_providers:
        return node.mode
    separator = ", " if node.mode == "parallel" else " -> "
    return separator.join(node.configured_providers)
