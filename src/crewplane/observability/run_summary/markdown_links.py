from __future__ import annotations

import os
import re
from collections.abc import Sequence
from html import escape
from pathlib import Path
from unicodedata import category
from urllib.parse import quote

from crewplane.observability.log_presentation.sanitize import sanitize_text
from crewplane.observability.text_layout import display_width, fit_text, pad_text

from .models import RunSummary


def markdown_text(value: object) -> str:
    text = escape(chart_text(str(value)), quote=False)
    for character in ("\\", "`", "[", "]", "*", "_", "|"):
        text = text.replace(character, "\\" + character)
    return text


def artifact_link(
    summary: RunSummary,
    path: Path | str,
    label: str,
    run_relative: bool = False,
) -> str:
    target = Path(path)
    base = summary.summary_path.parent
    if not base.is_absolute():
        return markdown_text(path)
    if not target.is_absolute():
        if not run_relative:
            return markdown_text(path)
        target = base.parent / target
    try:
        relative = Path(os.path.relpath(target, base)).as_posix()
    except ValueError:
        return markdown_text(path)
    return f"[{markdown_text(label)}]({quote(relative, safe='/.-_~')})"


def table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    """Join pre-escaped cells into a Markdown table without a trailing LF."""
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(row) + " |" for row in rows),
        ]
    )


def chart_text(value: str) -> str:
    """Flatten sanitized labels without escaping literal fenced content."""
    flattened = " ".join(sanitize_text(value).splitlines()).replace("\t", " ")
    return "".join(char for char in flattened if category(char) != "Cc")


def fenced_chart(chart: str) -> str:
    """Wrap a completed literal chart in a fence its labels cannot terminate."""
    longest = max(
        (len(match.group()) for match in re.finditer(r"`+", chart)), default=0
    )
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{chart}\n{fence}"


def aligned_chart(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    """Align a 120-cell token preview, preserving every numerical field."""
    widths = [
        max(display_width(row[i]) for row in (headers, *rows))
        for i in range(len(headers))
    ]
    available = 120 - sum(widths[1:]) - 2 * (len(headers) - 1)
    if available < 8:
        return fenced_chart(
            "\n\n".join(
                "\n".join(
                    fit_text(f"{label}: {value}", 120)
                    if index == 0
                    else f"{label}: {value}"
                    for index, (label, value) in enumerate(
                        zip(headers, row, strict=True)
                    )
                )
                for row in rows
            )
        )
    widths[0] = min(widths[0], available)
    lines = []
    for row in (headers, *rows):
        cells = [pad_text(fit_text(row[0], widths[0]), widths[0])]
        cells.extend(
            " " * (width - display_width(value)) + value
            for value, width in zip(row[1:], widths[1:], strict=True)
        )
        lines.append("  ".join(cells).rstrip())
    return fenced_chart("\n".join(lines))
