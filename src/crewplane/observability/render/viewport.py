from __future__ import annotations

from dataclasses import dataclass

from crewplane.observability.events import RunDashboardState
from crewplane.observability.layout import TopologyLayout
from crewplane.observability.text_layout import display_width


@dataclass(frozen=True)
class LaneViewport:
    lane_widths: list[int]
    visible_start: int
    visible_count: int
    divider: str

    @property
    def visible_end(self) -> int:
        return self.visible_start + self.visible_count - 1

    @property
    def divider_width(self) -> int:
        return display_width(self.divider)


@dataclass(frozen=True)
class DashboardRenderContext:
    state: RunDashboardState
    layout: TopologyLayout
    viewport: LaneViewport
    stream_lines_per_node: int
    node_live_lines: dict[str, list[str]]


@dataclass(frozen=True)
class StickyRenderState:
    sticky_node_by_lane: dict[int, str]
    primary_nodes_visible: set[str]
    hide_sticky_for_visible_primary: bool


def visible_lane_window(
    lane_count: int,
    width: int,
    now_seconds: float,
    divider: str,
    min_lane_width: int,
    rotate_interval_seconds: float,
) -> tuple[int, int]:
    if lane_count <= 0:
        return 0, 1

    divider_width = display_width(divider)
    max_visible = max(1, (width + divider_width) // (min_lane_width + divider_width))
    visible_count = min(lane_count, max_visible)
    if visible_count >= lane_count:
        return 0, visible_count

    window_count = lane_count - visible_count + 1
    rotate_interval = max(0.25, rotate_interval_seconds)
    window_index = int(now_seconds // rotate_interval) % window_count
    return window_index, visible_count


def lane_widths(width: int, visible_count: int, divider_width: int) -> list[int]:
    if visible_count <= 0:
        return [width]
    available = max(visible_count, width - divider_width * (visible_count - 1))
    base = available // visible_count
    remainder = available % visible_count
    return [base + (1 if index < remainder else 0) for index in range(visible_count)]


def select_wave_window(
    state: RunDashboardState,
    layout: TopologyLayout,
    height: int,
    row_height: int,
    row_overhead: int,
) -> tuple[list[int], int, int]:
    wave_count = len(layout.waves)
    if wave_count == 0:
        return [], 0, 0

    row_budget = max(1, (height - 4) // (row_height + row_overhead))
    if wave_count <= row_budget:
        return list(range(wave_count)), 0, 0

    active_wave_indexes = sorted(
        {
            placement.wave_index
            for node_id, placement in layout.placements.items()
            if state.nodes[node_id].status == "running"
        }
    )

    if active_wave_indexes:
        focus_start = max(0, min(active_wave_indexes) - 1)
        focus_end = focus_start + row_budget
        if max(active_wave_indexes) >= focus_end:
            focus_end = max(active_wave_indexes) + 1
            focus_start = max(0, focus_end - row_budget)
    else:
        unresolved_index = first_unresolved_wave(state, layout)
        focus_start = max(0, unresolved_index - 1)
        focus_end = focus_start + row_budget

    focus_end = min(wave_count, focus_end)
    focus_start = max(0, focus_end - row_budget)

    detail_waves = list(range(focus_start, focus_end))
    collapsed_above = focus_start
    collapsed_below = wave_count - focus_end
    return detail_waves, collapsed_above, collapsed_below


def first_unresolved_wave(state: RunDashboardState, layout: TopologyLayout) -> int:
    for wave_index, wave_nodes in enumerate(layout.waves):
        if any(
            state.nodes[node_id].status in {"pending", "running"}
            for node_id in wave_nodes
        ):
            return wave_index
    return max(0, len(layout.waves) - 1)
