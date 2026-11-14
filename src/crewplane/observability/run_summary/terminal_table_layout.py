from __future__ import annotations

from dataclasses import dataclass

from crewplane.observability.text_layout import display_width

CELL_PADDING = 1
COLUMN_PADDING = 2 * CELL_PADDING
EXECUTION_HEADER = "Execution"
MIN_STATUS_WIDTH = display_width(EXECUTION_HEADER)
DURATION_HEADER = "Duration"
CALL_HEADER = "Call"
COMBINED_CALL_HEADER = "Call / model"
PROVIDER_HEADER = "Model / requested reasoning"


@dataclass(frozen=True)
class DurationTableRow:
    """Literal display values shared by call sizing and rendering."""

    identity: str
    provider_model: str
    status: str
    duration: str
    duration_ms: int | None


@dataclass(frozen=True)
class DurationTableLayout:
    label_width: int
    provider_width: int
    status_width: int
    duration_width: int
    bar_width: int
    maximum_duration_ms: int
    stacked: bool = False


def duration_table_layout(
    rows: tuple[DurationTableRow, ...], width: int
) -> DurationTableLayout:
    """Reserve complete numeric/status cells before bounding preview labels."""
    maximum = max((row.duration_ms or 0 for row in rows), default=0)
    status = max(display_width("Status"), *(display_width(row.status) for row in rows))
    duration = max(
        display_width(DURATION_HEADER), *(display_width(row.duration) for row in rows)
    )
    separate = width >= 100
    natural_label = max(
        display_width(CALL_HEADER if separate else COMBINED_CALL_HEADER),
        *(display_width(row.identity) for row in rows),
    )
    natural_model = max(
        display_width(PROVIDER_HEADER),
        *(display_width(row.provider_model) for row in rows),
    )
    if not separate:
        natural_label = max(natural_label, natural_model)
    fixed = status + duration + (3 if separate else 2) * COLUMN_PADDING
    label_minimum = 8 + (8 if separate else 0)
    bars = width >= 60 and any(row.duration_ms is not None for row in rows)
    bar = (
        min(
            30,
            width
            - fixed
            - natural_label
            - (natural_model if separate else 0)
            - COLUMN_PADDING,
        )
        if bars
        else 0
    )
    if bars:
        bar = max(12, bar)
        if width - fixed - bar - COLUMN_PADDING < label_minimum:
            bar = 0
    available = width - fixed - (bar + COLUMN_PADDING if bar else 0)
    stacked = width < 40 or available < label_minimum
    label, model = natural_label, natural_model if separate else 0
    while label + model > max(1, available):
        if model > label:
            model -= 1
        else:
            label -= 1
    return DurationTableLayout(
        max(1, label), max(0, model), status, duration, bar, maximum, stacked
    )
