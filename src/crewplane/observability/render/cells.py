from __future__ import annotations

from crewplane.observability.events import NodeRuntimeState
from crewplane.observability.text_layout import (
    display_width,
    pad_text,
    slice_text,
)

from .text import ROW_HEIGHT, fit_cell
from .viewport import DashboardRenderContext, StickyRenderState


def node_cell_lines(
    node: NodeRuntimeState,
    cell_width: int,
    left_clipped: bool,
    right_clipped: bool,
    stream_lines_per_node: int,
    live_lines: list[str],
) -> list[str]:
    title = f"{node.node_id} [{node.mode}]"
    if left_clipped:
        title = f"< {title}"
    if right_clipped:
        title = f"{title} >"
    return runtime_node_cell_lines(
        title=title,
        summary=node_summary(node),
        latest_event=latest_node_event(node),
        cell_width=cell_width,
        stream_lines_per_node=stream_lines_per_node,
        live_lines=live_lines,
    )


def sticky_node_cell_lines(
    node: NodeRuntimeState,
    cell_width: int,
    stream_lines_per_node: int,
    live_lines: list[str],
) -> list[str]:
    return runtime_node_cell_lines(
        title=f"{node.node_id} [sticky]",
        summary=node_summary(node),
        latest_event=latest_node_event(node),
        cell_width=cell_width,
        stream_lines_per_node=stream_lines_per_node,
        live_lines=live_lines,
    )


def runtime_node_cell_lines(
    title: str,
    summary: str,
    latest_event: str,
    cell_width: int,
    stream_lines_per_node: int,
    live_lines: list[str],
) -> list[str]:
    lines = [
        fit_cell(title, cell_width),
        fit_cell(summary, cell_width),
        fit_cell(latest_event, cell_width),
    ]
    if stream_lines_per_node <= 0:
        return lines

    visible_log_lines = live_lines[-stream_lines_per_node:]
    padding_count = stream_lines_per_node - len(visible_log_lines)
    if padding_count > 0:
        lines.extend(pad_text("", cell_width) for _ in range(padding_count))
    lines.extend(fit_cell(line, cell_width) for line in visible_log_lines)
    return lines


def latest_node_event(node: NodeRuntimeState) -> str:
    if node.recent_events:
        return node.recent_events[-1]
    return f"state={node.status}"


def node_summary(node: NodeRuntimeState) -> str:
    if node.running_invocations:
        running = sorted(
            invocation.task_id
            for invocation in node.invocations.values()
            if invocation.status == "running"
        )
        primary = running[0]
        additional = len(running) - 1
        if additional:
            return f"RUN {primary} (+{additional})"
        return f"RUN {primary}"

    if node.failed_invocations:
        return (
            f"FAIL {node.failed_invocations}/"
            f"{max(node.total_invocations, node.failed_invocations)}"
        )
    if node.succeeded_invocations:
        return f"DONE {node.succeeded_invocations}/{node.total_invocations}"
    return node.status.upper()


def overlay(target: str, start: int, value: str) -> str:
    target_width = display_width(target)
    if target_width <= 0 or start >= target_width:
        return target

    replacement_width = min(max(0, target_width - max(0, start)), display_width(value))
    prefix = slice_text(target, 0, max(0, start))
    replacement = slice_text(value, 0, replacement_width)
    suffix_start = max(0, start) + replacement_width
    suffix = slice_text(target, suffix_start, max(0, target_width - suffix_start))
    return f"{prefix}{replacement}{suffix}"


def render_sticky_cells(
    lines: list[str],
    context: DashboardRenderContext,
    sticky_state: StickyRenderState,
    occupied_lanes: set[int],
) -> None:
    viewport = context.viewport
    lane_widths = viewport.lane_widths
    for lane in range(viewport.visible_start, viewport.visible_end + 1):
        if lane in occupied_lanes:
            continue
        sticky_node_id = sticky_state.sticky_node_by_lane.get(lane)
        if sticky_node_id is None:
            continue
        sticky_node = context.state.nodes.get(sticky_node_id)
        if sticky_node is None:
            continue

        local_lane = lane - viewport.visible_start
        cell_width = lane_widths[local_lane]
        cell_x = span_start_x(local_lane, lane_widths, viewport.divider_width)
        if (
            sticky_state.hide_sticky_for_visible_primary
            and sticky_node_id in sticky_state.primary_nodes_visible
        ):
            cell_lines = sticky_continuation_cell_lines(
                cell_width=cell_width,
                stream_lines_per_node=context.stream_lines_per_node,
                live_lines=context.node_live_lines.get(sticky_node_id, []),
            )
        else:
            cell_lines = sticky_node_cell_lines(
                node=sticky_node,
                cell_width=cell_width,
                stream_lines_per_node=context.stream_lines_per_node,
                live_lines=context.node_live_lines.get(sticky_node_id, []),
            )
        for line_index, cell_line in enumerate(cell_lines):
            lines[line_index] = overlay(lines[line_index], cell_x, cell_line)


def sticky_continuation_cell_lines(
    cell_width: int,
    stream_lines_per_node: int,
    live_lines: list[str],
) -> list[str]:
    lines = [pad_text("", cell_width) for _ in range(ROW_HEIGHT)]
    if stream_lines_per_node <= 0:
        return lines

    previous_end = max(0, len(live_lines) - stream_lines_per_node)
    previous_start = max(0, previous_end - stream_lines_per_node)
    continuation_lines = live_lines[previous_start:previous_end]

    padding_count = stream_lines_per_node - len(continuation_lines)
    if padding_count > 0:
        lines.extend(pad_text("", cell_width) for _ in range(padding_count))
    lines.extend(fit_cell(line, cell_width) for line in continuation_lines)
    return lines


def span_start_x(local_start: int, lane_widths: list[int], divider_width: int) -> int:
    return sum(lane_widths[:local_start]) + divider_width * local_start


def span_width(
    local_start: int,
    local_end: int,
    lane_widths: list[int],
    divider_width: int,
) -> int:
    lane_count = local_end - local_start + 1
    return sum(lane_widths[local_start : local_end + 1]) + divider_width * (
        lane_count - 1
    )
