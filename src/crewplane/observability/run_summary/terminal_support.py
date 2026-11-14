"""Literal and encoding-safe Rich primitives for final summaries."""

from __future__ import annotations

import os
from unicodedata import category

from rich.console import Console, RenderableType
from rich.table import Table
from rich.text import Text

from crewplane.observability.log_presentation.sanitize import sanitize_text
from crewplane.observability.text_layout import fit_text, wrap_text

from .terminal_table_layout import CELL_PADDING

_LABEL_LINE_LIMIT = 2


def styled(console: Console) -> bool:
    return bool(
        console.file.isatty()
        and console.is_terminal
        and console.color_system is not None
        and not console.no_color
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "").lower() not in {"dumb", "unknown"}
    )


def safe(value: object, console: Console) -> str:
    sanitized = " ".join(sanitize_text(value).splitlines()).replace("\t", " ")
    single_line = "".join(char for char in sanitized if category(char) != "Cc")
    return single_line.encode(console.encoding, errors="backslashreplace").decode(
        console.encoding
    )


def text(value: object, console: Console, style: str = "") -> Text:
    return Text(safe(value, console), style=style if styled(console) else "")


def wrapped(value: object, console: Console, style: str = "") -> Text:
    rendered = text(value, console, style)
    rendered.plain = "\n".join(wrap_text(rendered.plain, console.width))
    return rendered


def label(value: object, console: Console, style: str = "") -> Text:
    return wrapped(
        fit_text(safe(value, console), console.width * _LABEL_LINE_LIMIT),
        console,
        style,
    )


def heading(value: str, console: Console) -> Text:
    return label(value, console, "bold cyan")


def omitted(count: int, subject: str, console: Console) -> list[RenderableType]:
    return (
        [wrapped(f"{count} more {subject} in summary.md", console, "dim")]
        if count > 0
        else []
    )


def table(console: Console) -> Table:
    return Table(
        box=None,
        safe_box=True,
        padding=(0, CELL_PADDING),
        pad_edge=False,
        header_style="bold" if styled(console) else "",
        border_style="",
        style="",
        show_edge=False,
        collapse_padding=True,
    )
