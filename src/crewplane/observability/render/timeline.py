from __future__ import annotations

from collections.abc import Sequence

from crewplane.observability.layout import TopologyLayout
from crewplane.observability.text_layout import display_width

from .cells import (
    node_cell_lines,
    overlay,
    render_sticky_cells,
    span_start_x,
    span_width,
)
from .text import ROW_HEIGHT
from .viewport import DashboardRenderContext, StickyRenderState


def timeline_wave_separator_line(
    lane_widths: list[int],
    divider: str,
    wave_nodes: Sequence[str],
    layout: TopologyLayout,
    visible_start: int,
    visible_end: int,
) -> str:
    visible_count = len(lane_widths)
    if visible_count == 0:
        return ""

    lane_has_separator = [False for _ in range(visible_count)]
    for node_id in wave_nodes:
        placement = layout.placements[node_id]
        clipped_start = max(placement.lane_start, visible_start)
        clipped_end = min(placement.lane_end, visible_end)
        if clipped_start > clipped_end:
            continue
        for lane in range(clipped_start, clipped_end + 1):
            lane_has_separator[lane - visible_start] = True

    segments = [
        ("─" if lane_has_separator[index] else " ") * lane_width
        for index, lane_width in enumerate(lane_widths)
    ]
    separator = segments[0]
    for index in range(visible_count - 1):
        left_has_separator = lane_has_separator[index]
        right_has_separator = lane_has_separator[index + 1]
        joiner = (
            timeline_lane_separator(divider)
            if left_has_separator and right_has_separator
            else divider
        )
        separator += joiner + segments[index + 1]
    return separator


def timeline_lane_separator(divider: str) -> str:
    if "│" in divider:
        return divider.replace("│", "┼").replace(" ", "─")
    return "─" * max(1, display_width(divider))


def render_wave_lines(
    wave_nodes: Sequence[str],
    context: DashboardRenderContext,
    sticky_state: StickyRenderState,
) -> list[str]:
    viewport = context.viewport
    lane_widths = viewport.lane_widths
    base_line = viewport.divider.join(" " * lane_width for lane_width in lane_widths)
    row_height = ROW_HEIGHT + context.stream_lines_per_node
    lines = [base_line for _ in range(row_height)]
    occupied_lanes: set[int] = set()

    ordered_nodes = sorted(wave_nodes, key=context.layout.node_order.__getitem__)
    for node_id in ordered_nodes:
        placement = context.layout.placements[node_id]
        clipped_start = max(placement.lane_start, viewport.visible_start)
        clipped_end = min(placement.lane_end, viewport.visible_end)
        if clipped_start > clipped_end:
            continue
        occupied_lanes.update(range(clipped_start, clipped_end + 1))

        local_start = clipped_start - viewport.visible_start
        local_end = clipped_end - viewport.visible_start
        span_x = span_start_x(local_start, lane_widths, viewport.divider_width)
        cell_width = span_width(
            local_start, local_end, lane_widths, viewport.divider_width
        )
        left_clipped = placement.lane_start < viewport.visible_start
        right_clipped = placement.lane_end > viewport.visible_end

        cell_lines = node_cell_lines(
            node=context.state.nodes[node_id],
            cell_width=cell_width,
            left_clipped=left_clipped,
            right_clipped=right_clipped,
            stream_lines_per_node=context.stream_lines_per_node,
            live_lines=context.node_live_lines.get(node_id, []),
        )
        for line_index, cell_line in enumerate(cell_lines):
            lines[line_index] = overlay(lines[line_index], span_x, cell_line)

    render_sticky_cells(
        lines=lines,
        context=context,
        sticky_state=sticky_state,
        occupied_lanes=occupied_lanes,
    )
    return lines


def update_sticky_node_by_lane(
    sticky_node_by_lane: dict[int, str],
    wave_nodes: Sequence[str],
    layout: TopologyLayout,
) -> None:
    for node_id in wave_nodes:
        placement = layout.placements[node_id]
        for lane in range(placement.lane_start, placement.lane_end + 1):
            sticky_node_by_lane[lane] = node_id


def visible_wave_node_ids(
    wave_nodes: Sequence[str],
    layout: TopologyLayout,
    visible_start: int,
    visible_end: int,
) -> set[str]:
    visible_node_ids: set[str] = set()
    for node_id in wave_nodes:
        placement = layout.placements[node_id]
        if placement.lane_end < visible_start or placement.lane_start > visible_end:
            continue
        visible_node_ids.add(node_id)
    return visible_node_ids
