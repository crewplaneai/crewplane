from __future__ import annotations

from dataclasses import dataclass

from crewplane.observability.layout import TopologyLayout

SHORT_TRANSITIVE_PATH_LENGTH = 2


@dataclass(frozen=True)
class NodeGraphState:
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


def build_graph_states(
    ordered_node_ids: list[str],
    layout: TopologyLayout,
) -> list[NodeGraphState]:
    active_columns: list[str] = []
    graph_states: list[NodeGraphState] = []
    dependencies, dependents = render_dependency_maps(layout)
    for node_id in ordered_node_ids:
        fresh_root = is_fresh_root(node_id, active_columns, dependencies)
        node_column = node_column_for(
            active_columns=active_columns,
            node_id=node_id,
            fresh_root=fresh_root,
        )
        before_columns = tuple(active_columns)
        active_columns, dependent_columns = advance_columns(
            active_columns=active_columns,
            node_id=node_id,
            node_column=node_column,
            dependents=dependents,
            fresh_root=fresh_root,
            layout=layout,
        )
        graph_states.append(
            NodeGraphState(
                node_id=node_id,
                node_column=node_column,
                before_columns=before_columns,
                after_columns=tuple(active_columns),
                dependent_columns=tuple(dependent_columns),
                dependents=dependents.get(node_id, ()),
            )
        )
    return graph_states


def render_dependency_maps(
    layout: TopologyLayout,
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    dependencies = {
        node_id: tuple(
            dependency_id
            for dependency_id in dependency_ids
            if not is_short_transitive_shortcut(dependency_id, node_id, layout)
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


def is_short_transitive_shortcut(
    dependency_id: str,
    node_id: str,
    layout: TopologyLayout,
) -> bool:
    return (
        max_alternate_path_length(dependency_id, node_id, layout)
        == SHORT_TRANSITIVE_PATH_LENGTH
    )


def max_alternate_path_length(
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


def is_fresh_root(
    node_id: str,
    active_columns: list[str],
    dependencies: dict[str, tuple[str, ...]],
) -> bool:
    return not dependencies.get(node_id) and node_id not in active_columns


def node_column_for(
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


def advance_columns(
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

    primary_dependent = primary_dependent_for(
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


def primary_dependent_for(
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


def render_node_graph(
    graph_state: NodeGraphState,
    visible_columns: int,
) -> str:
    chars = graph_chars(visible_columns)
    continuing_targets = set(graph_state.after_columns)
    for column in range(min(visible_columns, len(graph_state.before_columns))):
        if column == graph_state.node_column:
            continue
        if column >= len(graph_state.after_columns):
            continue
        target = graph_state.before_columns[column]
        if target == graph_state.node_id or target not in continuing_targets:
            continue
        chars[column_index(column)] = "│"

    if graph_state.node_column < visible_columns:
        chars[column_index(graph_state.node_column)] = "●"
    else:
        chars[column_index(visible_columns - 1)] = "…"

    return "".join(chars).rstrip()


def render_connector_graph(
    graph_state: NodeGraphState,
    next_node_id: str | None,
    visible_columns: int,
) -> str | None:
    if not graph_state.after_columns:
        return None

    chars = graph_chars(visible_columns)
    should_render = False
    incoming_columns = incoming_columns_for(graph_state, next_node_id)

    if len(graph_state.dependent_columns) > 1:
        should_render = True
        fill_vertical_columns(chars, len(graph_state.after_columns), visible_columns)
        if len(incoming_columns) > 1:
            overlay_branch_and_join_span(chars, graph_state, incoming_columns)
        else:
            overlay_span(
                chars,
                list(graph_state.dependent_columns),
                middle_char="┬",
                end_char="┐",
            )
    elif next_node_id is not None:
        if len(incoming_columns) > 1:
            should_render = True
            fill_vertical_columns(
                chars, len(graph_state.after_columns), visible_columns
            )
            overlay_span(chars, incoming_columns, middle_char="┴", end_char="┘")
        elif (
            len(graph_state.dependents) == 1
            and graph_state.dependents[0] == next_node_id
        ):
            should_render = True
            fill_vertical_columns(
                chars, len(graph_state.after_columns), visible_columns
            )

    if not should_render:
        return None

    connector = "".join(chars).rstrip()
    return connector or None


def incoming_columns_for(
    graph_state: NodeGraphState,
    next_node_id: str | None,
) -> list[int]:
    if next_node_id is None:
        return []
    return [
        index
        for index, target in enumerate(graph_state.after_columns)
        if target == next_node_id
    ]


def overlay_branch_and_join_span(
    chars: list[str],
    graph_state: NodeGraphState,
    incoming_columns: list[int],
) -> None:
    span_columns = sorted({*graph_state.dependent_columns, *incoming_columns})
    overlay_span(chars, span_columns, middle_char="┬", end_char="┘")


def fill_vertical_columns(
    chars: list[str],
    column_count: int,
    visible_columns: int,
) -> None:
    for column in range(min(visible_columns, column_count)):
        chars[column_index(column)] = "│"


def overlay_span(
    chars: list[str],
    columns: list[int],
    middle_char: str,
    end_char: str,
) -> None:
    clipped_columns = sorted(
        {column for column in columns if 0 <= column_index(column) < len(chars)}
    )
    if len(clipped_columns) < 2:
        return

    start_column = clipped_columns[0]
    end_column = clipped_columns[-1]
    for char_index in range(column_index(start_column), column_index(end_column) + 1):
        if char_index % 2 == 1:
            chars[char_index] = "─"

    chars[column_index(start_column)] = "├"
    chars[column_index(end_column)] = end_char
    for column in clipped_columns[1:-1]:
        chars[column_index(column)] = middle_char


def graph_chars(visible_columns: int) -> list[str]:
    return [" " for _ in range(graph_width(visible_columns))]


def graph_width(visible_columns: int) -> int:
    return max(1, visible_columns * 2 - 1)


def column_budget(max_graph_width: int) -> int:
    return max(1, (max_graph_width + 1) // 2)


def column_index(column: int) -> int:
    return column * 2
