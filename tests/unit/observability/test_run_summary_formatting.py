from __future__ import annotations

import pytest

from crewplane.observability.run_summary.formatting import (
    format_call_duration,
    format_chart_duration,
    full_track,
)
from crewplane.observability.run_summary.terminal_table_layout import (
    DurationTableRow,
    duration_table_layout,
)


@pytest.mark.parametrize(
    ("value", "maximum", "width", "expected"),
    [
        (None, 10, 5, "     "),
        (0, 0, 5, "....."),
        (0, 10, 5, "....."),
        (1, 10, 5, "#...."),
        (5, 10, 5, "###.."),
        (1, 10**100, 5, "#...."),
        (10**100, 10**100, 5, "#####"),
        (5 * 10**99, 10**100, 5, "###.."),
    ],
)
def test_full_tracks_use_exact_half_up_rounding(
    value: int | None, maximum: int, width: int, expected: str
) -> None:
    assert full_track(value, maximum, width) == expected


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (None, "n/a"),
        (0, "0.0s"),
        (1, "<0.1s"),
        (99, "<0.1s"),
        (100, "0.1s"),
        (39_500, "39.5s"),
        (86_700, "1m 26.7s"),
        (3_723_400, "1h 02m 03.4s"),
        (59_949, "59.9s"),
        (59_950, "1m 00.0s"),
        (3_599_950, "1h 00m 00.0s"),
    ],
)
def test_chart_duration_rounds_before_decomposing_units(
    duration: int | None, expected: str
) -> None:
    assert format_chart_duration(duration) == expected


def test_complete_duration_formatter_preserves_millisecond_evidence() -> None:
    assert format_call_duration(80_125) == "80.125s"


def test_short_call_does_not_waste_long_model_label_budget() -> None:
    row = DurationTableRow(
        "n / t", "model-with-a-long-name / max", "succeeded", "1.0s", 1000
    )
    layout = duration_table_layout((row,), 120)
    assert layout.provider_width >= len(row.provider_model)
    assert layout.label_width < layout.provider_width
    assert layout.bar_width == 30


def test_oversize_numbers_force_stacking_instead_of_cropping() -> None:
    row = DurationTableRow("call", "model", "succeeded", "9" * 100 + "s", 1000)
    assert duration_table_layout((row,), 100).stacked
