from __future__ import annotations

from dataclasses import replace

import pytest

from crewplane.observability.run_summary.formatting import (
    format_call_duration,
    format_chart_duration,
    full_track,
)
from crewplane.observability.run_summary.markdown_links import chart_text
from crewplane.observability.run_summary.models import InvocationTimingSummary
from crewplane.observability.run_summary.terminal_table_layout import (
    DurationTableRow,
    duration_preview,
    duration_table_layout,
    duration_table_lines,
)
from crewplane.observability.text_layout import display_width


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
        "n", "t", "r1", "model-with-a-long-name / max", "succeeded", "1.0s", 1000
    )
    layout = duration_table_layout((row,), 120)
    assert layout.model_width >= len(row.model)
    assert layout.node_width < layout.model_width
    assert layout.bar_width == 20


def test_oversize_numbers_force_stacking_instead_of_cropping() -> None:
    row = DurationTableRow(
        "node", "call", "r1", "model", "succeeded", "9" * 100 + "s", 1000
    )
    assert duration_table_layout((row,), 100).stacked


def test_shared_node_path_preserves_iterations_and_rounds() -> None:
    timings = tuple(
        InvocationTimingSummary(
            f"files.i{iteration}.refactor-file-loop",
            "codex_reviewer_0",
            "codex",
            "gpt-6-astra",
            1,
            round_num,
            "succeeded",
            180000,
            "max",
        )
        for iteration, round_num in ((0, 1), (1, 1), (0, 2))
    )
    preview = duration_preview(timings, chart_text)
    layout = duration_table_layout(preview.rows, 120)
    rendered = "\n".join(duration_table_lines(preview.rows, layout))

    assert preview.context == (
        "Node path: files.{node}.refactor-file-loop",
        "Provider: codex",
    )
    assert [(row.node, row.call, row.round) for row in preview.rows] == [
        ("i0", "reviewer_0", "a1/r1"),
        ("i1", "reviewer_0", "a1/r1"),
        ("i0", "reviewer_0", "a1/r2"),
    ]
    assert "Model / reasoning" in rendered and "Relative time" in rendered
    assert "..." not in rendered.replace("." * 20, "")
    assert all(display_width(line) <= 120 for line in rendered.splitlines())


@pytest.mark.parametrize(
    ("tasks", "providers", "expected"),
    [
        (
            ("codex_executor_0", "codex_executor_1"),
            ("codex", "codex"),
            ("executor_0", "executor_1"),
        ),
        (
            ("codex_executor_0", "executor_0"),
            ("codex", "codex"),
            ("codex_executor_0", "executor_0"),
        ),
        (
            ("codex_executor_0", "claude_executor_0"),
            ("codex", "claude"),
            ("codex_executor_0", "claude_executor_0"),
        ),
    ],
)
def test_call_labels_keep_indices_and_avoid_provider_collisions(
    tasks: tuple[str, str], providers: tuple[str, str], expected: tuple[str, str]
) -> None:
    timings = tuple(
        InvocationTimingSummary("node", task, provider, None, None, 0, "failed", None)
        for task, provider in zip(tasks, providers, strict=True)
    )
    preview = duration_preview(timings, chart_text)
    assert tuple(row.call for row in preview.rows) == expected
    assert all(row.round == "r0" for row in preview.rows)
    assert ("Provider: codex" in preview.context) is (len(set(providers)) == 1)


def test_mixed_nodes_fit_full_names_with_twenty_cell_tracks() -> None:
    timings = tuple(
        InvocationTimingSummary(
            node,
            task,
            "codex",
            model,
            audit,
            round_num,
            "succeeded",
            duration,
            reasoning,
        )
        for node, task, model, audit, round_num, duration, reasoning in (
            (
                "implement.implement.build",
                "codex_executor_0",
                "gpt-6-astra",
                None,
                1,
                4394766,
                "max",
            ),
            (
                "implement.implement.review",
                "codex_reviewer_0",
                "gpt-6-astra",
                1,
                1,
                1244439,
                "max",
            ),
            (
                "plan.plan.init",
                "codex_executor_0",
                "gpt-6-astra",
                None,
                1,
                795757,
                "max",
            ),
            (
                "doc.docs-review-fix-loop",
                "codex_executor_0",
                "gpt-6.1-terra",
                None,
                1,
                176920,
                "xhigh",
            ),
        )
    )
    preview = duration_preview(timings, chart_text)
    wide = duration_table_lines(preview.rows, duration_table_layout(preview.rows, 120))
    assert max(display_width(line) for line in wide) == 115
    assert all(timing.node_id in "\n".join(wide) for timing in timings)
    assert "####################" in wide[1] and "1h 13m 14.8s" in wide[1]
    assert "a1/r1" in wide[2] and "######.............." in wide[2]

    narrow = duration_table_lines(preview.rows, duration_table_layout(preview.rows, 80))
    assert all(display_width(line) <= 80 for line in narrow)
    assert narrow[1].startswith("build ")
    assert narrow[3].startswith("review ")
    assert "gpt-6-astra / max" in narrow[2]
    assert "####################" in narrow[1]


def test_shortened_node_labels_keep_identical_leaf_names_distinct() -> None:
    rows = (
        DurationTableRow(
            "left.very-long-parent.build",
            "executor_0",
            "a1/r1",
            "model / max",
            "failed",
            "1.0s",
            1000,
        ),
        DurationTableRow(
            "right.very-long-parent.build",
            "executor_0",
            "a1/r1",
            "model / max",
            "failed",
            "1.0s",
            1000,
        ),
    )
    lines = duration_table_lines(rows, duration_table_layout(rows, 80))
    assert any(line.startswith("left.") for line in lines)
    assert any(line.startswith("right.") for line in lines)
    assert all(display_width(line) <= 80 for line in lines)


@pytest.mark.parametrize("width", [60, 80, 120])
@pytest.mark.parametrize("field", ["node", "call", "model"])
@pytest.mark.parametrize("character", ["x", "界"], ids=["ascii", "wide"])
def test_long_duration_labels_have_bounded_previews(
    width: int, field: str, character: str
) -> None:
    row = DurationTableRow(
        "node", "executor_0", "a1/r1", "model / high", "succeeded", "1m 20.1s", 80125
    )
    row = replace(row, **{field: character * 10_000})
    lines = duration_table_lines((row,), duration_table_layout((row,), width))
    rendered = "\n".join(lines)

    assert len(lines) <= 6
    assert "..." in rendered
    assert all(value in rendered for value in ("a1/r1", "succeeded", "1m 20.1s"))
    assert all(display_width(line) <= width for line in lines)
