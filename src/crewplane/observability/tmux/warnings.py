from __future__ import annotations

import sys
from collections.abc import Callable


def dispatch_tmux_warning(
    warning_sink: Callable[[str], None] | None,
    message: str,
) -> None:
    """Send a best-effort tmux warning to the configured sink or stderr."""

    if warning_sink is not None:
        try:
            warning_sink(message)
        except Exception:
            return
        return
    print(f"WARN: {message}", file=sys.stderr)
