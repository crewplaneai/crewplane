from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from itertools import takewhile, zip_longest

from crewplane.observability.text_layout import (
    display_width,
    fit_text,
    pad_text,
    wrap_text,
)

from .formatting import format_chart_duration, full_track, timing_model
from .models import InvocationTimingSummary

CELL_PADDING = 1
COLUMN_PADDING = 2 * CELL_PADDING
DURATION_HEADER = "Duration"
EXECUTION_HEADER = "Execution"
MIN_STATUS_WIDTH = display_width(EXECUTION_HEADER)


@dataclass(frozen=True)
class DurationTableRow:
    """Literal display values shared by call sizing and rendering."""

    node: str
    call: str
    round: str
    model: str
    status: str
    duration: str
    duration_ms: int | None


@dataclass(frozen=True)
class DurationPreview:
    rows: tuple[DurationTableRow, ...]
    context: tuple[str, ...]


@dataclass(frozen=True)
class DurationTableLayout:
    node_width: int
    call_width: int
    round_width: int
    model_width: int
    status_width: int
    duration_width: int
    bar_width: int
    maximum_duration_ms: int
    stacked: bool = False

    @property
    def columns(self) -> tuple[tuple[str, int], ...]:
        columns: tuple[tuple[str, int], ...] = (
            ("Node", self.node_width),
            ("Call", self.call_width),
            ("Round", self.round_width),
        )
        if self.model_width:
            columns += (("Model / reasoning", self.model_width),)
        columns += (("Status", self.status_width),)
        if self.bar_width:
            columns += (("Relative time", self.bar_width),)
        return columns + (("Duration", self.duration_width),)


def duration_preview(
    timings: tuple[InvocationTimingSummary, ...], sanitize: Callable[[str], str]
) -> DurationPreview:
    """Factor out shared context without merging distinct recorded identities."""
    node_labels, node_context = _node_labels(timings)
    providers = {timing.provider for timing in timings}
    provider = next(iter(providers)) if len(providers) == 1 else None
    tasks = {timing.task_id for timing in timings}
    calls = {
        task: task.removeprefix(f"{provider}_") if provider else task for task in tasks
    }
    if "" in calls.values() or len(set(calls.values())) != len(tasks):
        calls = {task: task for task in tasks}
    context = tuple(
        sanitize(value)
        for value in (node_context, f"Provider: {provider}" if provider else "")
        if value
    )
    return DurationPreview(
        tuple(
            DurationTableRow(
                sanitize(node_labels[timing.node_id]),
                sanitize(calls[timing.task_id]),
                _compact_round(timing),
                sanitize(timing_model(timing)),
                sanitize(timing.status),
                format_chart_duration(timing.duration_ms),
                timing.duration_ms,
            )
            for timing in timings
        ),
        context,
    )


def _common_segments(paths: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    return tuple(
        parts[0]
        for parts in takewhile(
            lambda parts: len(set(parts)) == 1, zip(*paths, strict=False)
        )
    )


def _node_labels(
    timings: tuple[InvocationTimingSummary, ...],
) -> tuple[dict[str, str], str]:
    nodes = tuple(dict.fromkeys(timing.node_id for timing in timings))
    paths = tuple(tuple(node.split(".")) for node in nodes)
    prefix = _common_segments(paths)[: min(map(len, paths)) - 1]
    remaining = tuple(path[len(prefix) :] for path in paths)
    suffix = _common_segments(tuple(path[::-1] for path in remaining))[
        : min(map(len, remaining)) - 1
    ][::-1]
    labels = {
        node: ".".join(path[len(prefix) : len(path) - len(suffix)])
        for node, path in zip(nodes, paths, strict=True)
    }
    context = (
        "Node path: " + ".".join((*prefix, "{node}", *suffix))
        if prefix or suffix
        else ""
    )
    return labels, context


def _compact_round(timing: InvocationTimingSummary) -> str:
    parts = (
        (f"a{timing.audit_round_num}", timing.audit_round_num),
        (f"r{timing.round_num}", timing.round_num),
    )
    return "/".join(label for label, value in parts if value is not None) or "-"


def duration_table_layout(
    rows: tuple[DurationTableRow, ...], width: int
) -> DurationTableLayout:
    """Reserve round/status/duration cells, then fit labels and a fixed scale."""
    maximum = max((row.duration_ms or 0 for row in rows), default=0)
    round_width = max(len("Round"), *(display_width(row.round) for row in rows))
    status = max(len("Status"), *(display_width(row.status) for row in rows))
    duration = max(len("Duration"), *(display_width(row.duration) for row in rows))
    separate_model = width >= 100
    bar = 20 if width >= 60 and any(row.duration_ms is not None for row in rows) else 0
    natural = (
        max(len("Node"), *(display_width(row.node) for row in rows)),
        max(len("Call"), *(display_width(row.call) for row in rows)),
        max(len("Model / reasoning"), *(display_width(row.model) for row in rows)),
    )
    labels = (
        min(28, natural[0]),
        min(20, natural[1]),
        min(32, natural[2]) if separate_model else 0,
    )
    minimum = (
        min(labels[0], 8),
        min(labels[1], 8),
        min(labels[2], len("Model / reasoning")),
    )
    fixed = round_width + status + duration
    available = (
        width
        - fixed
        - (4 + int(separate_model) + int(bool(bar))) * COLUMN_PADDING
        - bar
    )
    if sum(minimum) > available and bar:
        bar = 0
        available = width - fixed - (4 + int(separate_model)) * COLUMN_PADDING
    stacked = width < 60 or sum(minimum) > available
    fitted = list(labels)
    while sum(fitted) > available and not stacked:
        index = max(range(3), key=lambda index: fitted[index] - minimum[index])
        fitted[index] -= 1
    return DurationTableLayout(
        fitted[0],
        fitted[1],
        round_width,
        fitted[2],
        status,
        duration,
        bar,
        maximum,
        stacked,
    )


def duration_row_cells(
    row: DurationTableRow, layout: DurationTableLayout
) -> tuple[str, ...]:
    cells: tuple[str, ...] = (row.node, row.call, row.round)
    if layout.model_width:
        cells += (row.model,)
    cells += (row.status,)
    if layout.bar_width:
        cells += (
            full_track(row.duration_ms, layout.maximum_duration_ms, layout.bar_width),
        )
    return cells + (row.duration,)


def _unique_node_suffix(node: str, nodes: set[str]) -> str:
    parts = node.split(".")
    for count in range(1, len(parts) + 1):
        suffix = ".".join(parts[-count:])
        if all(
            other == node or (other != suffix and not other.endswith("." + suffix))
            for other in nodes
        ):
            return suffix
    return node


def duration_table_lines(
    rows: tuple[DurationTableRow, ...], layout: DurationTableLayout
) -> tuple[str, ...]:
    """Wrap bounded labels and put secondary models across the whole row."""
    widths = tuple(width for _, width in layout.columns)
    lines = list(
        _aligned_duration_lines(tuple(header for header, _ in layout.columns), widths)
    )
    nodes = {row.node for row in rows}
    chart_width = sum(widths) + COLUMN_PADDING * (len(widths) - 1)
    for row in rows:
        node = (
            _unique_node_suffix(row.node, nodes)
            if display_width(row.node) > layout.node_width
            else row.node
        )
        cells = (node, *duration_row_cells(row, layout)[1:])
        lines.extend(_aligned_duration_lines(cells, widths))
        if not layout.model_width:
            lines.extend(
                wrap_text(fit_text("  " + row.model, chart_width * 2), chart_width)
            )
    return tuple(lines)


def _aligned_duration_lines(
    cells: tuple[str, ...], widths: tuple[int, ...]
) -> tuple[str, ...]:
    wrapped = tuple(
        wrap_text(fit_text(value, width * 2), width)
        for value, width in zip(cells, widths, strict=True)
    )
    return tuple(
        "  ".join(
            " " * (width - display_width(value)) + value
            if index == len(widths) - 1
            else pad_text(value, width)
            for index, (value, width) in enumerate(zip(line, widths, strict=True))
        ).rstrip()
        for line in zip_longest(*wrapped, fillvalue="")
    )
