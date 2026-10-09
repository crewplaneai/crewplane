import os
from pathlib import Path


def append_event_log_line(path: Path, line: str) -> None:
    from crewplane.architecture.safe_files_windows import open_writable_file

    with (
        open_writable_file(path, append=True) as descriptor,
        os.fdopen(descriptor, "ab", closefd=False) as handle,
    ):
        handle.write(line.encode("utf-8"))
