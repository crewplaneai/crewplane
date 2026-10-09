"""Select the contained-file implementation at the shared I/O boundary."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SafeFileOperations:
    ensure_contained_directory: Callable[[Path, tuple[str, ...]], Path]
    contained_directory: Callable[[Path, tuple[str, ...]], Path | None]
    contained_regular_file: Callable[[Path, tuple[str, ...]], Path | None]
    ensure_regular_file: Callable[[Path], Path]
    replace_contained_file: Callable[[Path, tuple[str, ...], Path], Path]
    open_regular_file: Callable[[Path], AbstractContextManager[int]]
    open_destination: Callable[[Path], AbstractContextManager[int]]
    open_contained_file: Callable[[Path, str], AbstractContextManager[int]]
    signature_timestamp: Callable[[os.stat_result], int]


def safe_file_operations() -> SafeFileOperations:
    if os.name == "nt":
        from . import safe_files_windows

        return SafeFileOperations(
            safe_files_windows.ensure_contained_directory,
            safe_files_windows.contained_directory,
            safe_files_windows.contained_regular_file,
            safe_files_windows.ensure_regular_file,
            safe_files_windows.replace_contained_file,
            safe_files_windows.open_regular_file,
            safe_files_windows.open_writable_file,
            safe_files_windows.open_contained_file,
            safe_files_windows.signature_timestamp,
        )
    from . import safe_files_posix

    return SafeFileOperations(
        safe_files_posix.ensure_contained_directory,
        safe_files_posix.contained_directory,
        safe_files_posix.contained_regular_file,
        safe_files_posix.ensure_single_link_regular_file,
        safe_files_posix.replace_contained_file,
        safe_files_posix.open_regular_file,
        safe_files_posix.open_destination,
        safe_files_posix.open_contained_file,
        safe_files_posix.signature_timestamp,
    )
