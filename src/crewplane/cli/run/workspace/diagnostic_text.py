from __future__ import annotations

from collections.abc import Sequence


def summarize_paths(paths: Sequence[str]) -> str:
    """Render at most five paths plus an omitted-count suffix."""

    selected = paths[:5]
    suffix = (
        f" (+{len(paths) - len(selected)} more)" if len(paths) > len(selected) else ""
    )
    return ", ".join(selected) + suffix
