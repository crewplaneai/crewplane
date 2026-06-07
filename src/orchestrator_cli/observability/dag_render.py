from __future__ import annotations

from dataclasses import dataclass

from orchestrator_cli.observability.events import NodeRuntimeState, RunDashboardState
from orchestrator_cli.observability.layout import TopologyLayout
from orchestrator_cli.observability.node_order import topological_node_order
from orchestrator_cli.observability.status_icons import status_icon
from orchestrator_cli.observability.text_layout import (
    display_width,
    fit_text,
    pad_text,
)
from orchestrator_cli.observability.timing import format_elapsed_seconds

MAX_GRAPH_WIDTH = 12
SHORT_TRANSITIVE_PATH_LENGTH = 2


@dataclass(frozen=True)
class DagRenderConfig:
    """Configuration for compact DAG summary rendering."""

    max_graph_width: int = MAX_GRAPH_WIDTH


@dataclass(frozen=True)
class _NodeGraphState:
    node_id: str
    node_column: int
    before_columns: tuple[str, ...]
    after_columns: tuple[str, ...]
    dependent_columns: tuple[int, ...]
    dependents: tuple[str, ...]

    @property
    def column_count(self) -> int:
        return max(
            len(self.before_columns),
            len(self.after_columns),
            self.node_column + 1,
        )


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

    graph_states = _build_graph_states(ordered_node_ids, layout)
    max_columns = max(
        (graph_state.column_count for graph_state in graph_states),
        default=1,
    )
    visible_columns = min(max(1, max_columns), _column_budget(config.max_graph_width))
    hidden_columns = max(0, max_columns - visible_columns)
    graph_width = _graph_width(visible_columns)

    lines: list[str] = []
    for index, graph_state in enumerate(graph_states):
        node = state.nodes[graph_state.node_id]
        graph = _render_node_graph(
            graph_state=graph_state,
            visible_columns=visible_columns,
        )
        marker = "▸" if graph_state.node_id == selected_node_id else " "
        graph_column = pad_text(graph, graph_width)
        node_column_width = max(24, display_width(graph_state.node_id))
        node_column = pad_text(graph_state.node_id, node_column_width)
        line = f"{marker}{graph_column} {node_column} {_node_meta(node, now)}"
        lines.append(fit_text(line.rstrip(), width))

        next_node_id = (
            graph_states[index + 1].node_id if index + 1 < len(graph_states) else None
        )
        connector = _render_connector_graph(
            graph_state=graph_state,
            next_node_id=next_node_id,
            visible_columns=visible_columns,
        )
        if connector is not None:
            lines.append(fit_text(f" {connector}", width))

    if hidden_columns:
        lines.append(fit_text(f"... +{hidden_columns} more", width))
    return lines


def _build_graph_states(
    ordered_node_ids: list[str],
    layout: TopologyLayout,
) -> list[_NodeGraphState]:
    active_columns: list[str] = []
    graph_states: list[_NodeGraphState] = []
    dependencies, dependents = _render_dependency_maps(layout)
    for node_id in ordered_node_ids:
        fresh_root = _is_fresh_root(node_id, active_columns, dependencies)
        node_column = _node_column(
            active_columns=active_columns,
            node_id=node_id,
            fresh_root=fresh_root,
        )
        before_columns = tuple(active_columns)
        active_columns, dependent_columns = _advance_columns(
            active_columns=active_columns,
            node_id=node_id,
            node_column=node_column,
            dependents=dependents,
            fresh_root=fresh_root,
            layout=layout,
        )
        graph_states.append(
            _NodeGraphState(
                node_id=node_id,
                node_column=node_column,
                before_columns=before_columns,
                after_columns=tuple(active_columns),
                dependent_columns=tuple(dependent_columns),
                dependents=dependents.get(node_id, ()),
            )
        )
    return graph_states


def _render_dependency_maps(
    layout: TopologyLayout,
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    dependencies = {
        node_id: tuple(
            dependency_id
            for dependency_id in dependency_ids
            if not _is_short_transitive_shortcut(dependency_id, node_id, layout)
        )
        for node_id, dependency_ids in layout.dependencies.items()
    }
    dependents = {node_id: [] for node_id in layout.node_order}
    for node_id, dependency_ids in dependencies.items():
        for dependency_id in dependency_ids:
            dependents[dependency_id].append(node_id)

    return dependencies, {
        node_id: tuple(sorted(node_ids, key=layout.node_order.__getitem__))
        for node_id, node_ids in dependents.items()
    }


def _is_short_transitive_shortcut(
    dependency_id: str,
    node_id: str,
    layout: TopologyLayout,
) -> bool:
    return (
        _max_alternate_path_length(dependency_id, node_id, layout)
        == SHORT_TRANSITIVE_PATH_LENGTH
    )


def _max_alternate_path_length(
    dependency_id: str,
    node_id: str,
    layout: TopologyLayout,
) -> int:
    stack = [
        (dependent_id, 1)
        for dependent_id in layout.dependents.get(dependency_id, ())
        if dependent_id != node_id
    ]
    best_distance_by_node: dict[str, int] = {}
    max_distance = 0
    while stack:
        current_node_id, distance = stack.pop()
        if best_distance_by_node.get(current_node_id, 0) >= distance:
            continue
        best_distance_by_node[current_node_id] = distance
        if current_node_id == node_id:
            max_distance = max(max_distance, distance)
            continue
        stack.extend(
            (dependent_id, distance + 1)
            for dependent_id in layout.dependents.get(current_node_id, ())
        )
    return max_distance


def _is_fresh_root(
    node_id: str,
    active_columns: list[str],
    dependencies: dict[str, tuple[str, ...]],
) -> bool:
    return not dependencies.get(node_id) and node_id not in active_columns


def _node_column(
    active_columns: list[str],
    node_id: str,
    fresh_root: bool,
) -> int:
    if fresh_root:
        return len(active_columns)
    for index, target in enumerate(active_columns):
        if target == node_id:
            return index
    return len(active_columns)


def _advance_columns(
    active_columns: list[str],
    node_id: str,
    node_column: int,
    dependents: dict[str, tuple[str, ...]],
    fresh_root: bool,
    layout: TopologyLayout,
) -> tuple[list[str], list[int]]:
    if fresh_root:
        next_columns = list(active_columns)
        insertion_index = node_column
    else:
        next_columns = [target for target in active_columns if target != node_id]
        insertion_index = node_column
    node_dependents = list(dependents.get(node_id, ()))
    if not node_dependents:
        return next_columns, []

    primary_dependent = _primary_dependent(
        dependents=node_dependents,
        layout=layout,
    )
    next_columns.insert(insertion_index, primary_dependent)
    dependent_columns = [insertion_index]
    insert_at = insertion_index + 1
    for dependent_id in node_dependents:
        if dependent_id == primary_dependent:
            continue
        next_columns.insert(insert_at, dependent_id)
        dependent_columns.append(insert_at)
        insert_at += 1
    return next_columns, dependent_columns


def _primary_dependent(
    dependents: list[str],
    layout: TopologyLayout,
) -> str:
    if len(dependents) == 1:
        return dependents[0]
    return min(
        dependents,
        key=lambda dependent_id: (
            layout.placements[dependent_id].wave_index,
            layout.placements[dependent_id].lane_start,
            layout.node_order[dependent_id],
        ),
    )


def _render_node_graph(
    graph_state: _NodeGraphState,
    visible_columns: int,
) -> str:
    chars = _graph_chars(visible_columns)
    continuing_targets = set(graph_state.after_columns)
    for column in range(min(visible_columns, len(graph_state.before_columns))):
        if column == graph_state.node_column:
            continue
        if column >= len(graph_state.after_columns):
            continue
        target = graph_state.before_columns[column]
        if target == graph_state.node_id or target not in continuing_targets:
            continue
        chars[_column_index(column)] = "│"

    if graph_state.node_column < visible_columns:
        chars[_column_index(graph_state.node_column)] = "●"
    else:
        chars[_column_index(visible_columns - 1)] = "…"

    return "".join(chars).rstrip()


def _render_connector_graph(
    graph_state: _NodeGraphState,
    next_node_id: str | None,
    visible_columns: int,
) -> str | None:
    if not graph_state.after_columns:
        return None

    chars = _graph_chars(visible_columns)
    should_render = False
    incoming_columns = _incoming_columns(graph_state, next_node_id)

    if len(graph_state.dependent_columns) > 1:
        should_render = True
        _fill_vertical_columns(chars, len(graph_state.after_columns), visible_columns)
        if len(incoming_columns) > 1:
            _overlay_branch_and_join_span(chars, graph_state, incoming_columns)
        else:
            _overlay_span(
                chars,
                list(graph_state.dependent_columns),
                middle_char="┬",
                end_char="┐",
            )
    elif next_node_id is not None:
        if len(incoming_columns) > 1:
            should_render = True
            _fill_vertical_columns(
                chars, len(graph_state.after_columns), visible_columns
            )
            _overlay_span(chars, incoming_columns, middle_char="┴", end_char="┘")
        elif (
            len(graph_state.dependents) == 1
            and graph_state.dependents[0] == next_node_id
        ):
            should_render = True
            _fill_vertical_columns(
                chars, len(graph_state.after_columns), visible_columns
            )

    if not should_render:
        return None

    connector = "".join(chars).rstrip()
    return connector or None


def _incoming_columns(
    graph_state: _NodeGraphState,
    next_node_id: str | None,
) -> list[int]:
    if next_node_id is None:
        return []
    return [
        index
        for index, target in enumerate(graph_state.after_columns)
        if target == next_node_id
    ]


def _overlay_branch_and_join_span(
    chars: list[str],
    graph_state: _NodeGraphState,
    incoming_columns: list[int],
) -> None:
    span_columns = sorted({*graph_state.dependent_columns, *incoming_columns})
    _overlay_span(chars, span_columns, middle_char="┬", end_char="┘")


def _fill_vertical_columns(
    chars: list[str],
    column_count: int,
    visible_columns: int,
) -> None:
    for column in range(min(visible_columns, column_count)):
        chars[_column_index(column)] = "│"


def _overlay_span(
    chars: list[str],
    columns: list[int],
    middle_char: str,
    end_char: str,
) -> None:
    clipped_columns = sorted(
        {column for column in columns if 0 <= _column_index(column) < len(chars)}
    )
    if len(clipped_columns) < 2:
        return

    start_column = clipped_columns[0]
    end_column = clipped_columns[-1]
    for char_index in range(_column_index(start_column), _column_index(end_column) + 1):
        if char_index % 2 == 1:
            chars[char_index] = "─"

    chars[_column_index(start_column)] = "├"
    chars[_column_index(end_column)] = end_char
    for column in clipped_columns[1:-1]:
        chars[_column_index(column)] = middle_char


def _graph_chars(visible_columns: int) -> list[str]:
    return [" " for _ in range(_graph_width(visible_columns))]


def _graph_width(visible_columns: int) -> int:
    return max(1, visible_columns * 2 - 1)


def _column_budget(max_graph_width: int) -> int:
    return max(1, (max_graph_width + 1) // 2)


def _column_index(column: int) -> int:
    return column * 2


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
