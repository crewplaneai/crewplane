import os
from pathlib import Path


def append_event_log_line(path: Path, line: str) -> None:
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(line.encode("utf-8"))
