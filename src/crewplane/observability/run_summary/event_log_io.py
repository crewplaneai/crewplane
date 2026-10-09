"""Select append I/O while the run logger owns formatting and synchronization."""

import os
from collections.abc import Callable
from pathlib import Path


def event_log_appender() -> Callable[[Path, str], None]:
    if os.name == "nt":
        from .event_log_windows import append_event_log_line
    else:
        from .event_log_posix import append_event_log_line
    return append_event_log_line
