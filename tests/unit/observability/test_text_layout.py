from __future__ import annotations

import pytest

from orchestrator_cli.observability.text_layout import (
    display_width,
    fit_text,
    wrap_text,
)


@pytest.mark.parametrize(
    ("text", "expected_width"),
    [
        ("plain", 5),
        ("🙂", 2),
        ("漢字", 4),
        ("e\u0301", 1),
        ("", 0),
    ],
)
def test_display_width_matches_terminal_cell_width(
    text: str,
    expected_width: int,
) -> None:
    assert display_width(text) == expected_width


@pytest.mark.parametrize(
    ("text", "width", "expected"),
    [
        ("abcdef", 0, ""),
        ("abcdef", 1, "."),
        ("abcdef", 2, ".."),
        ("abcdef", 3, "..."),
        ("abcdef", 4, "a..."),
        ("🙂hello", 4, "..."),
        ("🙂hello", 5, "🙂..."),
        ("漢字hello", 6, "漢..."),
        ("e\u0301clair", 5, "e\u0301c..."),
    ],
)
def test_fit_text_handles_narrow_ellipsis_and_wide_graphemes(
    text: str,
    width: int,
    expected: str,
) -> None:
    fitted = fit_text(text, width)
    assert fitted == expected
    assert display_width(fitted) <= max(0, width)


def test_wrap_text_preserves_blank_lines_and_uses_cell_width() -> None:
    assert wrap_text("", 4) == [""]
    assert wrap_text("alpha\n\nbeta", 4) == ["alph", "a", "", "beta"]
    assert wrap_text("🙂🙂🙂", 4) == ["🙂🙂", "🙂"]
    assert wrap_text("漢字abc", 4) == ["漢字", "abc"]


@pytest.mark.parametrize(
    ("text", "width", "expected"),
    [
        ("🙂", 1, ["."]),
        ("漢", 1, ["."]),
        ("🙂a", 1, [".", "a"]),
        ("漢a", 1, [".", "a"]),
    ],
)
def test_wrap_text_never_returns_lines_wider_than_requested_width(
    text: str,
    width: int,
    expected: list[str],
) -> None:
    wrapped = wrap_text(text, width)
    assert wrapped == expected
    assert all(display_width(line) <= width for line in wrapped)


def test_wrap_text_returns_blank_rows_for_zero_width() -> None:
    assert wrap_text("alpha", 0) == [""]
    assert wrap_text("alpha\n\nbeta", 0) == ["", "", ""]
