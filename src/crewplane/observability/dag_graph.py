from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from crewplane.observability.layout import TopologyLayout

SHORT_TRANSITIVE_PATH_LENGTH = 2
GraphColumn = str | None


@dataclass(frozen=True)
class NodeGraphState:
    node_id: str
    node_column: int
    before_columns: tuple[GraphColumn, ...]
    after_columns: tuple[GraphColumn, ...]
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
    active_columns: list[GraphColumn] = []
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
    dependencies = prune_short_transitive_dependencies(layout)
    dependencies = prune_post_fanin_transitive_dependencies(dependencies, layout)
    return dependencies, dependent_map_for(dependencies, layout)


def prune_short_transitive_dependencies(
    layout: TopologyLayout,
) -> dict[str, tuple[str, ...]]:
    return {
        node_id: tuple(
            dependency_id
            for dependency_id in dependency_ids
            if alternate_path_length(dependency_id, node_id, layout.dependents)
            != SHORT_TRANSITIVE_PATH_LENGTH
        )
        for node_id, dependency_ids in layout.dependencies.items()
    }


def prune_post_fanin_transitive_dependencies(
    dependencies: dict[str, tuple[str, ...]],
    layout: TopologyLayout,
) -> dict[str, tuple[str, ...]]:
    dependents = dependent_map_for(dependencies, layout)
    fanin_nodes = {
        node_id
        for node_id, dependency_ids in dependencies.items()
        if len(dependency_ids) > 1
    }
    return {
        node_id: tuple(
            dependency_id
            for dependency_id in dependency_ids
            if not has_alternate_path_through_fanin(
                dependency_id,
                node_id,
                dependents,
                fanin_nodes,
            )
        )
        for node_id, dependency_ids in dependencies.items()
    }


def dependent_map_for(
    dependencies: dict[str, tuple[str, ...]],
    layout: TopologyLayout,
) -> dict[str, tuple[str, ...]]:
    dependents = {node_id: [] for node_id in layout.node_order}
    for node_id, dependency_ids in dependencies.items():
        for dependency_id in dependency_ids:
            dependents[dependency_id].append(node_id)

    return {
        node_id: tuple(sorted(node_ids, key=layout.node_order.__getitem__))
        for node_id, node_ids in dependents.items()
    }


def alternate_path_length(
    dependency_id: str,
    node_id: str,
    dependents: Mapping[str, Sequence[str]],
) -> int:
    stack = [
        (dependent_id, 1)
        for dependent_id in dependents.get(dependency_id, ())
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
            for dependent_id in dependents.get(current_node_id, ())
        )
    return max_distance


def has_alternate_path_through_fanin(
    dependency_id: str,
    node_id: str,
    dependents: dict[str, tuple[str, ...]],
    fanin_nodes: set[str],
) -> bool:
    stack = [
        (dependent_id, False)
        for dependent_id in dependents.get(dependency_id, ())
        if dependent_id != node_id
    ]
    visited: set[tuple[str, bool]] = set()
    while stack:
        current_node_id, passed_fanin = stack.pop()
        if current_node_id == node_id:
            if passed_fanin:
                return True
            continue
        if (current_node_id, passed_fanin) in visited:
            continue
        visited.add((current_node_id, passed_fanin))
        next_passed_fanin = passed_fanin or current_node_id in fanin_nodes
        stack.extend(
            (dependent_id, next_passed_fanin)
            for dependent_id in dependents.get(current_node_id, ())
        )
    return False


def is_fresh_root(
    node_id: str,
    active_columns: list[GraphColumn],
    dependencies: dict[str, tuple[str, ...]],
) -> bool:
    return not dependencies.get(node_id) and node_id not in active_columns


def node_column_for(
    active_columns: list[GraphColumn],
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
    active_columns: list[GraphColumn],
    node_id: str,
    node_column: int,
    dependents: dict[str, tuple[str, ...]],
    fresh_root: bool,
    layout: TopologyLayout,
) -> tuple[list[GraphColumn], list[int]]:
    if fresh_root:
        next_columns = list(active_columns)
        insertion_index = node_column
    else:
        next_columns = [
            None if target == node_id else target for target in active_columns
        ]
        insertion_index = node_column
    node_dependents = list(dependents.get(node_id, ()))
    if not node_dependents:
        return trim_trailing_empty_columns(next_columns), []

    primary_dependent = primary_dependent_for(
        dependents=node_dependents,
        layout=layout,
    )
    ordered_dependents = [
        primary_dependent,
        *(
            dependent_id
            for dependent_id in node_dependents
            if dependent_id != primary_dependent
        ),
    ]
    dependent_columns: list[int] = []
    insert_at = insertion_index
    reuse_existing_dependents = fresh_root and len(node_dependents) > 1
    for dependent_id in ordered_dependents:
        existing_column = (
            existing_column_for(next_columns, dependent_id)
            if reuse_existing_dependents
            else None
        )
        if existing_column is None:
            place_active_column(next_columns, insert_at, dependent_id)
            dependent_columns.append(insert_at)
            insert_at += 1
            continue
        dependent_columns.append(existing_column)
    if node_column not in dependent_columns and node_column >= len(next_columns):
        dependent_columns.append(node_column)
    return trim_trailing_empty_columns(next_columns), dependent_columns


def existing_column_for(
    columns: list[GraphColumn],
    target_id: str,
) -> int | None:
    for index, existing_target_id in enumerate(columns):
        if existing_target_id == target_id:
            return index
    return None


def place_active_column(
    columns: list[GraphColumn],
    column_index: int,
    target_id: str,
) -> None:
    if column_index < len(columns) and columns[column_index] is None:
        columns[column_index] = target_id
        return
    columns.insert(column_index, target_id)


def trim_trailing_empty_columns(columns: list[GraphColumn]) -> list[GraphColumn]:
    trimmed_columns = list(columns)
    while trimmed_columns and trimmed_columns[-1] is None:
        trimmed_columns.pop()
    return trimmed_columns


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
        if target is None:
            continue
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
        fill_vertical_columns(chars, graph_state.after_columns, visible_columns)
        if len(incoming_columns) > 1:
            overlay_branch_and_join_span(chars, graph_state, incoming_columns)
        else:
            overlay_span(
                chars,
                list(graph_state.dependent_columns),
                middle_char="┬",
                end_char=branch_span_end_char(graph_state),
            )
    elif next_node_id is not None:
        if len(incoming_columns) > 1:
            should_render = True
            fill_vertical_columns(chars, graph_state.after_columns, visible_columns)
            overlay_span(chars, incoming_columns, middle_char="┴", end_char="┘")
        elif (
            len(graph_state.dependents) == 1
            and graph_state.dependents[0] == next_node_id
        ):
            should_render = True
            fill_vertical_columns(chars, graph_state.after_columns, visible_columns)

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


def branch_span_end_char(graph_state: NodeGraphState) -> str:
    if graph_state.node_column >= len(graph_state.after_columns):
        return "┘"
    return "┐"


def fill_vertical_columns(
    chars: list[str],
    columns: tuple[GraphColumn, ...],
    visible_columns: int,
) -> None:
    for column, target in enumerate(columns[:visible_columns]):
        if target is None:
            continue
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
    for column in range(start_column + 1, end_column):
        char_index = column_index(column)
        if column not in clipped_columns:
            chars[char_index] = "┼" if chars[char_index] == "│" else "─"


def graph_chars(visible_columns: int) -> list[str]:
    return [" " for _ in range(graph_width(visible_columns))]


def graph_width(visible_columns: int) -> int:
    return max(1, visible_columns * 2 - 1)


def column_budget(max_graph_width: int) -> int:
    return max(1, (max_graph_width + 1) // 2)


def column_index(column: int) -> int:
    return column * 2
