"""Copy selected generated files with source validation and failure cleanup."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from crewplane.architecture import safe_files_windows, windows_file_handles
from crewplane.architecture.safe_file_reads import (
    copy_regular_file,
    stable_file_signature,
)
from crewplane.architecture.safe_files import (
    ensure_contained_directory,
)

from .snapshot_io import (
    copy_snapshot_bytes,
    validate_generated_file_snapshot_relative_path,
)
from .snapshot_policy import GeneratedFileSnapshotCandidate


def copy_generated_file_snapshot_candidate(
    candidate: GeneratedFileSnapshotCandidate, target: Path, root: Path
) -> tuple[int, str]:
    validate_generated_file_snapshot_relative_path(candidate)
    with safe_files_windows.protected_directory(target.parent) as parent:
        identity: tuple[int, int] | None = None
        try:
            with ExitStack() as handles:
                descriptor = handles.enter_context(
                    _open_windows_snapshot_source(root, candidate)
                )
                initial = _validate_windows_snapshot_source(descriptor, candidate)
                destination = handles.enter_context(
                    safe_files_windows.open_writable_file(target)
                )
                identity = windows_file_handles.descriptor_identity(destination, target)
                return _copy_windows_snapshot_bytes(
                    descriptor, candidate, destination, initial
                )
        except (OSError, ValueError, RuntimeError):
            if identity is not None:
                safe_files_windows.delete_matching_entry(parent, target.name, identity)
            raise


def _validate_windows_snapshot_source(
    descriptor: int, candidate: GeneratedFileSnapshotCandidate
) -> os.stat_result:
    initial = os.fstat(descriptor)
    if (initial.st_dev, initial.st_ino, initial.st_size) != (
        candidate.source_device,
        candidate.source_inode,
        candidate.size_bytes,
    ):
        raise RuntimeError(
            f"Generated-file snapshot source changed before copying: {candidate.relative_label}"
        )
    return initial


def _copy_windows_snapshot_bytes(
    source_descriptor: int,
    candidate: GeneratedFileSnapshotCandidate,
    destination_descriptor: int,
    initial: os.stat_result,
) -> tuple[int, str]:
    with os.fdopen(destination_descriptor, "wb", closefd=False) as stream:
        result = copy_snapshot_bytes(source_descriptor, candidate, stream)
    if stable_file_signature(initial) != stable_file_signature(
        os.fstat(source_descriptor)
    ):
        raise RuntimeError(
            f"Generated-file snapshot source changed while copying: {candidate.relative_label}"
        )
    return result


@contextmanager
def _open_windows_snapshot_source(
    root: Path, candidate: GeneratedFileSnapshotCandidate
) -> Iterator[int]:
    with ExitStack() as handles:
        try:
            descriptor = handles.enter_context(
                safe_files_windows.open_regular_file(
                    root.joinpath(*candidate.relative_path.parts)
                )
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                "Generated-file snapshot source changed before copying: "
                f"{candidate.relative_label}"
            ) from exc
        yield descriptor


def copy_generated_result(generated_file: Path, target: Path) -> None:
    from crewplane.architecture.safe_files_windows import (
        protected_file,
        rename_contained_file,
        temporary_binary_file,
    )
    from crewplane.architecture.windows_file_handles import descriptor_identity

    with (
        safe_files_windows.protected_directory(target.parent, create=True),
        temporary_binary_file(target.parent, ".generated-file-", ".tmp") as (
            temporary_path,
            stream,
        ),
    ):
        identity = descriptor_identity(stream.fileno(), temporary_path)
        stream.close()
        copy_regular_file(generated_file, temporary_path)
        with protected_file(generated_file), protected_file(temporary_path):
            shutil.copymode(generated_file, temporary_path)
        rename_contained_file(temporary_path, target, identity)


def reset_directory(path: Path) -> None:
    try:
        safe_files_windows.reset_directory(path)
    except ValueError as exc:
        raise RuntimeError(
            f"Generated-file source path is not a directory: {path.as_posix()}"
        ) from exc


def prepare_directory(root: Path, relative_path: Path) -> Path:
    return ensure_contained_directory(root, relative_path.as_posix())
