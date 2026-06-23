from __future__ import annotations

from rich.cells import cell_len, set_cell_size, split_graphemes


def display_width(text: str) -> int:
    """Return the terminal cell width for one line of text."""

    return cell_len(text)


def fit_text(text: str, width: int, ellipsis: str = "...") -> str:
    """Fit text within a target display width using a cell-safe ellipsis."""

    safe_width = max(0, width)
    if safe_width == 0:
        return ""
    if display_width(text) <= safe_width:
        return text

    ellipsis_width = display_width(ellipsis)
    if ellipsis_width <= 0:
        return slice_text(text, 0, safe_width)
    if safe_width <= ellipsis_width:
        return slice_text(ellipsis, 0, safe_width)

    content_width = safe_width - ellipsis_width
    return f"{slice_text(text, 0, content_width)}{ellipsis}"


def pad_text(text: str, width: int) -> str:
    """Crop or pad text to exactly the requested display width."""

    safe_width = max(0, width)
    if safe_width == 0:
        return ""
    return set_cell_size(text, safe_width)


def slice_text(text: str, start: int, width: int) -> str:
    """Slice text by terminal cells without splitting graphemes."""

    safe_start = max(0, start)
    safe_width = max(0, width)
    if safe_width == 0:
        return ""

    spans, _ = split_graphemes(text)
    parts: list[str] = []
    cursor = 0
    end = safe_start + safe_width

    for span_start, span_end, cell_width in spans:
        next_cursor = cursor + cell_width
        if next_cursor <= safe_start:
            cursor = next_cursor
            continue
        if cursor >= end:
            break
        if cursor < safe_start or next_cursor > end:
            cursor = next_cursor
            continue
        parts.append(text[span_start:span_end])
        cursor = next_cursor

    return "".join(parts)


def wrap_text(text: str, width: int) -> list[str]:
    """Wrap text into cell-safe lines while preserving blank logical lines."""

    safe_width = max(0, width)
    return [
        wrapped_line
        for logical_line in text.split("\n")
        for wrapped_line in _wrap_logical_line(logical_line, safe_width)
    ]


def _wrap_logical_line(text: str, width: int) -> list[str]:
    if width == 0:
        return [""]
    if text == "":
        return [""]

    spans, _ = split_graphemes(text)
    wrapped_lines: list[str] = []
    current_parts: list[str] = []
    current_width = 0

    for span_start, span_end, cell_width in spans:
        grapheme = text[span_start:span_end]
        if cell_width > width:
            if current_parts:
                wrapped_lines.append("".join(current_parts))
                current_parts = []
                current_width = 0
            wrapped_lines.append(fit_text(grapheme, width))
            continue
        if current_width + cell_width > width:
            wrapped_lines.append("".join(current_parts))
            current_parts = [grapheme]
            current_width = cell_width
            continue
        current_parts.append(grapheme)
        current_width += cell_width

    if current_parts:
        wrapped_lines.append("".join(current_parts))

    return wrapped_lines or [""]
