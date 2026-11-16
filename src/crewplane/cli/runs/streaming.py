"""Bounded raw UTF-8 streaming of one pinned, append-only saved log."""

from __future__ import annotations

import codecs
import errno
import io
import os
import stat
import sys
import time
from pathlib import Path
from typing import TextIO

from crewplane.architecture.safe_file_reads import open_shared_regular_file
from crewplane.architecture.safe_files import (
    is_safe_relative_path,
    is_single_link_regular_file,
    path_has_symlink_component,
)
from crewplane.core.platform import is_native_windows


class SavedLogStreamError(RuntimeError):
    """A selected saved log could not be read safely."""


def stream_saved_log(
    root: Path, path: Path, follow: bool = False, output: TextIO | None = None
) -> None:
    target = sys.stdout if output is None else output
    try:
        if isinstance(target, io.TextIOWrapper):
            target.reconfigure(newline="")
        relative = path.absolute().relative_to(root.absolute()).as_posix()
        if not is_safe_relative_path(relative):
            raise ValueError("Log path must be contained without traversal.")
        if path_has_symlink_component(path) or not path.is_file():
            raise SavedLogStreamError(f"Saved log is missing or unsafe: {path}")
        with open_shared_regular_file(path) as descriptor:
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
            _write_output(output, decoder.decode(chunk))
            continue
        if not follow:
            _write_output(output, decoder.decode(b"", final=True))
            return
        _write_output(output, "")
        time.sleep(0.1)


def _write_output(output: TextIO, text: str) -> None:
    try:
        output.write(text)
        output.flush()
    except OSError as exc:
        if (
            is_native_windows()
            and exc.errno == errno.EINVAL
            and stat.S_ISFIFO(os.fstat(output.fileno()).st_mode)
        ):
            raise BrokenPipeError("Saved log output pipe was closed.") from exc
        raise


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
    if path_has_symlink_component(path) or not is_single_link_regular_file(current):
        raise SavedLogStreamError(f"Saved log path became unsafe: {path}")
    opened = os.fstat(descriptor)
    if not is_single_link_regular_file(opened):
        raise SavedLogStreamError(f"Saved log is no longer a safe regular file: {path}")
    consumed = os.lseek(descriptor, 0, os.SEEK_CUR)
    if min(current.st_size, opened.st_size) < max(observed_size, consumed):
        raise SavedLogStreamError(f"Saved log was truncated: {path}")
    return max(observed_size, current.st_size, opened.st_size)
