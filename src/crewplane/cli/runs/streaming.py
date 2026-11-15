"""Bounded raw UTF-8 streaming of one pinned, append-only saved log."""

from __future__ import annotations

import codecs
import os
import sys
import time
from pathlib import Path
from typing import TextIO

from crewplane.architecture.safe_file_reads import open_regular_file
from crewplane.architecture.safe_files import (
    contained_regular_file,
    is_safe_relative_path,
    is_single_link_regular_file,
)


class SavedLogStreamError(RuntimeError):
    """A selected saved log could not be read safely."""


def stream_saved_log(
    root: Path, path: Path, follow: bool = False, output: TextIO | None = None
) -> None:
    target = sys.stdout if output is None else output
    try:
        relative = path.absolute().relative_to(root.absolute()).as_posix()
        if not is_safe_relative_path(relative):
            raise ValueError("Log path must be contained without traversal.")
        if contained_regular_file(root, relative) is None:
            raise SavedLogStreamError(f"Saved log is missing or unsafe: {path}")
        with open_regular_file(path) as descriptor:
            _stream_descriptor(root, relative, descriptor, follow, target)
    except KeyboardInterrupt:
        return
    except BrokenPipeError:
        if output is None:
            # Prevent Python from retrying the failed stdout buffer at shutdown.
            with open(os.devnull, "w", encoding="utf-8") as sink:
                os.dup2(sink.fileno(), target.fileno())
        return
    except (OSError, ValueError) as exc:
        raise SavedLogStreamError(
            f"Cannot safely read saved log {path}: {exc}"
        ) from exc


def _stream_descriptor(
    root: Path, relative: str, descriptor: int, follow: bool, output: TextIO
) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")()
    initial = os.fstat(descriptor)
    observed_size = initial.st_size
    while True:
        observed_size = _verify_file(root, relative, descriptor, initial, observed_size)
        chunk = os.read(descriptor, 64 * 1024)
        if chunk:
            output.write(decoder.decode(chunk))
            output.flush()
            continue
        if not follow:
            output.write(decoder.decode(b"", final=True))
            output.flush()
            return
        output.flush()
        time.sleep(0.1)


def _verify_file(
    root: Path,
    relative: str,
    descriptor: int,
    initial: os.stat_result,
    observed_size: int,
) -> int:
    path = root / relative
    try:
        current = path.lstat()
    except FileNotFoundError as exc:
        raise SavedLogStreamError(f"Saved log was deleted: {path}") from exc
    if (current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino):
        raise SavedLogStreamError(f"Saved log was replaced: {path}")
    if contained_regular_file(root, relative) is None:
        raise SavedLogStreamError(f"Saved log path became unsafe: {path}")
    opened = os.fstat(descriptor)
    if not is_single_link_regular_file(opened):
        raise SavedLogStreamError(f"Saved log is no longer a safe regular file: {path}")
    consumed = os.lseek(descriptor, 0, os.SEEK_CUR)
    if min(current.st_size, opened.st_size) < max(observed_size, consumed):
        raise SavedLogStreamError(f"Saved log was truncated: {path}")
    return max(observed_size, current.st_size, opened.st_size)
