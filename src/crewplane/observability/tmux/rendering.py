from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from crewplane.observability.dag_render import render_dag_summary
from crewplane.observability.events import (
    InvocationRuntimeState,
    NodeRuntimeState,
)
from crewplane.observability.log_presentation import LogPresentationSnapshot
from crewplane.observability.text_layout import fit_text, wrap_text
from crewplane.observability.timing import format_elapsed_seconds
from crewplane.observability.tmux.log_tail import LogSnapshot
from crewplane.observability.tmux.selected_invocation import (
    PreparedSelectedInvocation,
)
from crewplane.observability.tmux.viewport import viewport_dag_lines
from crewplane.observability.types import DashboardSnapshot


@dataclass(frozen=True)
class SelectedOutputRenderContext:
    nodes: Mapping[str, NodeRuntimeState]
    selected_node_id: str | None
    width: int
    pane_height: int
    log_tail_lines: int | None
    quiet_after_seconds: float
    monotonic_now: float
    prepared_invocation: PreparedSelectedInvocation | None = None


def render_left_dashboard(
    snapshot: DashboardSnapshot,
    selected_node_id: str | None,
    width: int,
    height: int,
    inspect_mode: bool,
    now: float,
) -> list[str]:
    dag_lines = render_dag_summary(
        state=snapshot.state,
        layout=snapshot.layout,
        selected_node_id=selected_node_id,
        width=width,
        now=now,
    )
    selected_row = _selected_dag_row(dag_lines)
    if height <= 2:
        return _fit_lines_to_width(
            viewport_dag_lines(dag_lines, height, selected_row),
            width,
        )

    footer = _dashboard_footer(inspect_mode)
    show_spacers = height >= 5
    chrome_height = 2 + (2 if show_spacers else 0)
    dag_height = max(1, height - chrome_height)
    visible_dag_lines = viewport_dag_lines(dag_lines, dag_height, selected_row)

    lines = [fit_text("DAG Summary", width)]
    if show_spacers:
        lines.append("")
    lines.extend(_fit_lines_to_width(visible_dag_lines, width))
    if show_spacers:
        lines.append("")
    lines.append(fit_text(footer, width))
    return lines[: max(0, height)]


def _dashboard_footer(inspect_mode: bool) -> str:
    if inspect_mode:
        return "[Log Inspect] [r] raw  [f] formatted  [Esc] return  [q] quit"
    return "[↑/↓] select  [Enter] inspect  [r] raw inspect  [q] quit"


def right_pane_title(
    mode: str,
    selected_node_id: str | None,
    inspect_node_id: str | None,
    inspect_view: str | None = None,
) -> str:
    if mode == "inspect":
        view_suffix = f" ({inspect_view})" if inspect_view else ""
        if inspect_node_id:
            return f"Node Log: {inspect_node_id}{view_suffix}"
        return f"Node Log{view_suffix}"
    if selected_node_id is None:
        return "Node Output"
    return f"Node Output: {selected_node_id}"


def render_selected_output(context: SelectedOutputRenderContext) -> list[str]:
    if context.selected_node_id is None:
        return _wrap_pane_lines(
            ["Node Output", "", "No nodes to display."],
            context.width,
        )

    node = context.nodes[context.selected_node_id]
    lines = [
        *_wrap_pane_line(f"Node Output: {context.selected_node_id}", context.width),
        *_wrap_pane_line(f"Status: {node.status}", context.width),
        "",
    ]

    prepared_invocation = context.prepared_invocation
    if prepared_invocation is None:
        lines.extend(_wrap_pane_line(_node_waiting_message(node), context.width))
        return lines

    invocation = prepared_invocation.invocation
    lines.extend(_wrap_pane_lines(_invocation_header_lines(invocation), context.width))
    lines.append("")
    if prepared_invocation.log_unavailable_message is not None:
        lines.extend(
            _wrap_pane_line(
                prepared_invocation.log_unavailable_message,
                context.width,
            )
        )
        return lines
    if prepared_invocation.log_snapshot is None:
        lines.extend(
            _wrap_pane_line(
                "Log file unavailable for this invocation.",
                context.width,
            )
        )
        return lines

    _append_log_snapshot_lines(
        lines=lines,
        invocation=invocation,
        log_snapshot=prepared_invocation.log_snapshot,
        presentation_snapshot=prepared_invocation.presentation_snapshot,
        context=context,
    )
    return lines


def _node_waiting_message(node: NodeRuntimeState) -> str:
    if node.status == "pending":
        return "Waiting for dependencies to complete..."
    if node.status == "blocked":
        return f"Blocked: {_blocked_reason(node)}"
    return "No invocation logs yet."


def _append_log_snapshot_lines(
    lines: list[str],
    invocation: InvocationRuntimeState,
    log_snapshot: LogSnapshot,
    presentation_snapshot: LogPresentationSnapshot | None,
    context: SelectedOutputRenderContext,
) -> None:
    if invocation.status == "running" and not log_snapshot.tail_lines:
        _append_running_metadata(lines, invocation, log_snapshot, context)
        lines.extend(
            _wrap_pane_line("Awaiting first output from provider...", context.width)
        )
        return

    if _is_quiet_running_invocation(
        invocation=invocation,
        log_snapshot=log_snapshot,
        quiet_after_seconds=context.quiet_after_seconds,
    ):
        _append_quiet_running_message(lines, invocation, log_snapshot, context)

    display_lines = _display_log_lines(presentation_snapshot, log_snapshot)
    wrapped_tail_lines = _wrap_pane_lines(display_lines, context.width)
    if context.log_tail_lines is None:
        available_tail_rows = max(1, context.pane_height - len(lines))
        wrapped_tail_lines = wrapped_tail_lines[-available_tail_rows:]
    if wrapped_tail_lines:
        lines.extend(wrapped_tail_lines)
        return
    lines.extend(_wrap_pane_line("No log output yet.", context.width))


def _display_log_lines(
    presentation_snapshot: LogPresentationSnapshot | None,
    log_snapshot: LogSnapshot,
) -> list[str]:
    if presentation_snapshot is None:
        return list(log_snapshot.tail_lines)
    lines = [notice.message for notice in presentation_snapshot.notices]
    lines.extend(presentation_snapshot.lines)
    return lines


def _append_running_metadata(
    lines: list[str],
    invocation: InvocationRuntimeState,
    log_snapshot: LogSnapshot,
    context: SelectedOutputRenderContext,
) -> None:
    lines.extend(
        _wrap_pane_lines(
            _running_metadata_lines(
                invocation=invocation,
                log_snapshot=log_snapshot,
                monotonic_now=context.monotonic_now,
            ),
            context.width,
        )
    )
    if len(lines) > 3:
        lines.append("")


def _append_quiet_running_message(
    lines: list[str],
    invocation: InvocationRuntimeState,
    log_snapshot: LogSnapshot,
    context: SelectedOutputRenderContext,
) -> None:
    _append_running_metadata(lines, invocation, log_snapshot, context)
    quiet_label = format_elapsed_seconds(log_snapshot.updated_age_seconds)
    lines.extend(
        _wrap_pane_lines(
            [
                f"No new output for {quiet_label}.",
                "Provider still running; waiting for new output.",
                "",
            ],
            context.width,
        )
    )


def _blocked_reason(node: NodeRuntimeState) -> str:
    if not node.recent_events:
        return "Blocked by unsatisfied dependencies."
    latest = node.recent_events[-1]
    if latest.startswith("BLOCKED "):
        return latest[len("BLOCKED ") :]
    return latest


def _invocation_header_lines(invocation: InvocationRuntimeState) -> list[str]:
    round_label = (
        f"audit{invocation.audit_round_num}/round{invocation.round_num}"
        if invocation.audit_round_num is not None and invocation.round_num is not None
        else (
            f"round{invocation.round_num}"
            if invocation.round_num is not None
            else "round?"
        )
    )
    return [
        (
            f"{invocation.provider}/{invocation.role}/{invocation.task_id} "
            f"({round_label}) [{invocation.status}]"
        )
    ]


def _running_metadata_lines(
    invocation: InvocationRuntimeState,
    log_snapshot: LogSnapshot,
    monotonic_now: float,
) -> list[str]:
    lines: list[str] = []
    running_for_seconds = _invocation_elapsed_seconds(
        started_at=invocation.started_at,
        monotonic_now=monotonic_now,
    )
    if running_for_seconds is not None:
        lines.append(f"Running for {format_elapsed_seconds(running_for_seconds)}")
    lines.append(
        "Log file: "
        f"{_format_byte_size(log_snapshot.size_bytes)} "
        f"(updated {format_elapsed_seconds(log_snapshot.updated_age_seconds)} ago)"
    )
    return lines


def _invocation_elapsed_seconds(
    started_at: float | None,
    monotonic_now: float,
) -> float | None:
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        return None
    return max(0.0, monotonic_now - float(started_at))


def _is_quiet_running_invocation(
    invocation: InvocationRuntimeState,
    log_snapshot: LogSnapshot,
    quiet_after_seconds: float,
) -> bool:
    return (
        invocation.status == "running"
        and bool(log_snapshot.tail_lines)
        and log_snapshot.updated_age_seconds >= quiet_after_seconds
    )


def _format_byte_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"

    size = float(size_bytes)
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}"

    return f"{size_bytes} B"


def _wrap_pane_lines(values: list[str], width: int) -> list[str]:
    wrapped: list[str] = []
    for value in values:
        wrapped.extend(_wrap_pane_line(value, width))
    return wrapped


def _wrap_pane_line(value: str, width: int) -> list[str]:
    return wrap_text(value, width)


def _fit_lines_to_width(lines: list[str], width: int) -> list[str]:
    return [fit_text(line, width) for line in lines]


def _selected_dag_row(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if line.startswith("▸"):
            return index
    return None
