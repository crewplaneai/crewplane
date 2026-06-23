from __future__ import annotations

import math
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from crewplane.observability.events import RunDashboardState
from crewplane.observability.layout import TopologyLayout
from crewplane.observability.text_layout import display_width, fit_text

from .header import counters_line, header_line
from .text import clamped_stream_lines, trim_to_height, wave_row_height
from .timeline import (
    render_wave_lines,
    timeline_wave_separator_line,
    update_sticky_node_by_lane,
    visible_wave_node_ids,
)
from .viewport import (
    DashboardRenderContext,
    LaneViewport,
    StickyRenderState,
    lane_widths,
    select_wave_window,
    visible_lane_window,
)

DEFAULT_DIVIDER = " │ "
DEFAULT_MIN_LANE_WIDTH = 28
DEFAULT_ROTATE_SECONDS = 2.0
DisplayMode = Literal["waves", "timeline"]


@dataclass(frozen=True)
class RenderConfig:
    """Configuration for text dashboard rendering."""

    min_lane_width: int = DEFAULT_MIN_LANE_WIDTH
    divider: str = DEFAULT_DIVIDER
    rotate_interval_seconds: float = DEFAULT_ROTATE_SECONDS
    stream_lines_per_node: int = 0
    display_mode: DisplayMode = "timeline"

    def __post_init__(self) -> None:
        if self.min_lane_width < 1:
            raise ValueError("RenderConfig.min_lane_width must be greater than 0.")
        if not isinstance(self.divider, str):
            raise ValueError("RenderConfig.divider must be a string.")
        if not math.isfinite(self.rotate_interval_seconds):
            raise ValueError("RenderConfig.rotate_interval_seconds must be finite.")
        if self.rotate_interval_seconds <= 0:
            raise ValueError(
                "RenderConfig.rotate_interval_seconds must be greater than 0."
            )
        if self.stream_lines_per_node < 0:
            raise ValueError(
                "RenderConfig.stream_lines_per_node must be greater than or equal to 0."
            )
        if self.display_mode not in {"waves", "timeline"}:
            raise ValueError("RenderConfig.display_mode must be 'waves' or 'timeline'.")


def render_dashboard_text(
    state: RunDashboardState,
    layout: TopologyLayout,
    width: int,
    height: int,
    now: float | None = None,
    node_live_lines: dict[str, list[str]] | None = None,
    config: RenderConfig = RenderConfig(),
) -> str:
    """Render a workflow dashboard snapshot as terminal-safe text."""

    if width <= 0 or height <= 0:
        return ""

    render_now = monotonic() if now is None else now
    safe_width = width
    safe_height = height
    visible_start, visible_count = visible_lane_window(
        lane_count=layout.lane_count,
        width=safe_width,
        now_seconds=render_now,
        divider=config.divider,
        min_lane_width=config.min_lane_width,
        rotate_interval_seconds=config.rotate_interval_seconds,
    )
    widths = lane_widths(
        width=safe_width,
        visible_count=visible_count,
        divider_width=display_width(config.divider),
    )

    show_wave_headers = config.display_mode == "waves"
    detail_waves, collapsed_above, collapsed_below = select_wave_window(
        state=state,
        layout=layout,
        height=safe_height,
        row_height=wave_row_height(config.stream_lines_per_node),
        row_overhead=(2 if show_wave_headers else 1),
    )

    lines = [
        fit_text(header_line(state), safe_width),
        fit_text(counters_line(state), safe_width),
        "",
    ]
    sticky_node_by_lane: dict[int, str] = {}
    primary_nodes_visible: set[str] = set()
    viewport = LaneViewport(
        lane_widths=widths,
        visible_start=visible_start,
        visible_count=visible_count,
        divider=config.divider,
    )
    render_context = DashboardRenderContext(
        state=state,
        layout=layout,
        viewport=viewport,
        stream_lines_per_node=clamped_stream_lines(config.stream_lines_per_node),
        node_live_lines=node_live_lines or {},
    )
    sticky_render_state = StickyRenderState(
        sticky_node_by_lane=sticky_node_by_lane,
        primary_nodes_visible=primary_nodes_visible,
        hide_sticky_for_visible_primary=not show_wave_headers,
    )
    if detail_waves:
        first_visible_wave = detail_waves[0]
        for wave_index in range(first_visible_wave):
            update_sticky_node_by_lane(
                sticky_node_by_lane,
                layout.waves[wave_index],
                layout,
            )

    for display_index, wave_index in enumerate(detail_waves):
        if not show_wave_headers and display_index > 0:
            lines.append(
                fit_text(
                    timeline_wave_separator_line(
                        lane_widths=widths,
                        divider=config.divider,
                        wave_nodes=layout.waves[wave_index],
                        layout=layout,
                        visible_start=visible_start,
                        visible_end=visible_start + visible_count - 1,
                    ),
                    safe_width,
                )
            )
        wave_nodes = layout.waves[wave_index]
        if show_wave_headers:
            lines.append(fit_text(f"Wave {wave_index + 1}", safe_width))
        lines.extend(
            fit_text(line, safe_width)
            for line in render_wave_lines(
                wave_nodes=wave_nodes,
                context=render_context,
                sticky_state=sticky_render_state,
            )
        )
        if show_wave_headers:
            lines.append("")
        primary_nodes_visible.update(
            visible_wave_node_ids(
                wave_nodes=wave_nodes,
                layout=layout,
                visible_start=visible_start,
                visible_end=visible_start + visible_count - 1,
            )
        )
        update_sticky_node_by_lane(sticky_node_by_lane, wave_nodes, layout)

    if collapsed_above or collapsed_below:
        collapsed_label = "Collapsed waves" if show_wave_headers else "Collapsed rows"
        lines.append(
            fit_text(
                f"{collapsed_label}: +{collapsed_above} above, +{collapsed_below} below",
                safe_width,
            )
        )

    hidden_lanes = layout.lane_count - visible_count
    if hidden_lanes > 0:
        visible_end = visible_start + visible_count - 1
        lines.append(
            fit_text(
                f"Showing lanes {visible_start + 1}-{visible_end + 1} "
                f"of {layout.lane_count} (auto-fit, rotating)",
                safe_width,
            )
        )

    return trim_to_height(lines, safe_width, safe_height)
