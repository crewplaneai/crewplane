"""Select complete generated-file operations within the artifact owner."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .snapshot_policy import GeneratedFileSnapshotCandidate


@dataclass(frozen=True)
class GeneratedFileOperations:
    copy_snapshot: Callable[
        [GeneratedFileSnapshotCandidate, Path, Path], tuple[int, str]
    ]
    copy_result: Callable[[Path, Path], None]
    reset_directory: Callable[[Path], None]
    prepare_directory: Callable[[Path, Path], Path]


def generated_file_operations() -> GeneratedFileOperations:
    if os.name == "nt":
        from . import io_windows

        return GeneratedFileOperations(
            io_windows.copy_generated_file_snapshot_candidate,
            io_windows.copy_generated_result,
            io_windows.reset_directory,
            io_windows.prepare_directory,
        )
    from . import io_posix

    return GeneratedFileOperations(
        io_posix.copy_generated_file_snapshot_candidate,
        io_posix.copy_generated_result,
        io_posix.reset_directory,
        io_posix.prepare_directory,
    )
