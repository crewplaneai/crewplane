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
PROVIDER_HEADER = "Provider / model"

_BAR_WIDTH_THRESHOLD = 60
_PROVIDER_COLUMN_WIDTH = 80
_MAX_BAR_WIDTH = 20


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


def duration_table_layout(
    rows: tuple[DurationTableRow, ...], width: int
) -> DurationTableLayout:
    maximum = max(row.duration_ms or 0 for row in rows)
    bars = (
        width >= _BAR_WIDTH_THRESHOLD
        and maximum > 0
        and sum(row.duration_ms is not None for row in rows) > 1
    )
    bar_width = min(_MAX_BAR_WIDTH, width // 5) if bars else 0
    status_width = max(MIN_STATUS_WIDTH, *(display_width(row.status) for row in rows))
    duration_width = max(
        display_width(DURATION_HEADER), *(display_width(row.duration) for row in rows)
    )
    separate_provider = width >= _PROVIDER_COLUMN_WIDTH
    column_count = 3 + int(bars) + int(separate_provider)
    padding = (column_count - 1) * COLUMN_PADDING
    available = max(1, width - status_width - duration_width - bar_width - padding)
    provider_width = (
        min(
            max(
                display_width(PROVIDER_HEADER),
                *(display_width(row.provider_model) for row in rows),
            ),
            max(1, available // 3),
        )
        if separate_provider
        else 0
    )
    labels = [row.identity for row in rows]
    if not separate_provider:
        labels.extend(row.provider_model for row in rows)
    label_width = min(
        max(
            display_width(COMBINED_CALL_HEADER),
            *(display_width(value) for value in labels),
        ),
        max(1, available - provider_width),
    )
    return DurationTableLayout(
        label_width,
        provider_width,
        status_width,
        duration_width,
        bar_width,
        maximum,
    )
