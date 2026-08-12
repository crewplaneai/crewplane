from __future__ import annotations

from typing import NotRequired, TypedDict


class SourceSpan(TypedDict):
    """One-based source line span with optional zero-based columns."""

    start_line: int
    end_line: int
    start_column: NotRequired[int]
    end_column: NotRequired[int]


class TokenRawSpan(TypedDict):
    """Zero-based half-open token offsets within its prompt segment."""

    start: int
    end: int
