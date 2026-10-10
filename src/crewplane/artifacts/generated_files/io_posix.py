"""Copy selected generated files with source validation and failure cleanup."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

from crewplane.architecture.safe_file_reads import (
    copy_regular_file,
)
from crewplane.architecture.safe_files import path_is_symlink

from .snapshot_io import (
    copy_snapshot_bytes,
    validate_generated_file_snapshot_relative_path,
)
from .snapshot_policy import GeneratedFileSnapshotCandidate


def copy_generated_file_snapshot_candidate(
    candidate: GeneratedFileSnapshotCandidate,
    target: Path,
    resolved_workspace_root: Path,
) -> tuple[int, str]:
    """Copy a selected workspace file and hash the exact destination bytes.

    Args:
        candidate: Selected file with a safe relative path and its recorded
            device, inode, and byte count under the workspace root.
        target: File in a caller-prepared, safe snapshot directory. Its parent
            must exist; opening it may create or truncate it.
        resolved_workspace_root: Resolved root used to open the source through
            the candidate's relative path.

    Returns:
        A ``(byte_count, sha256_hex)`` tuple for the copied bytes.

    Raises:
        RuntimeError: The source path is unsafe or source validation fails.
        OSError: File access, copying, or cleanup fails.

    Acquired descriptors and streams are released on every exit. OSError or
    RuntimeError failures trigger target unlinking; cleanup errors propagate.
    """
    source_descriptor: int | None = None
    try:
        source_descriptor = _open_generated_file_snapshot_candidate(
            resolved_workspace_root,
            candidate,
        )
        return _copy_open_generated_file_snapshot_candidate(
            source_descriptor,
            candidate,
            target,
        )
    except (OSError, RuntimeError):
        target.unlink(missing_ok=True)
        raise
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)


def _open_generated_file_snapshot_candidate(
    resolved_workspace_root: Path,
    candidate: GeneratedFileSnapshotCandidate,
) -> int:
    validate_generated_file_snapshot_relative_path(candidate)
    parent_descriptor = _open_generated_file_snapshot_parent(
        resolved_workspace_root,
        candidate.relative_path.parent,
        candidate.relative_label,
    )
    try:
        return _open_generated_file_snapshot_source(parent_descriptor, candidate)
    finally:
        os.close(parent_descriptor)


def _open_generated_file_snapshot_parent(
    resolved_workspace_root: Path,
    relative_parent: Path,
    relative_label: str,
) -> int:
    current_descriptor = _open_generated_file_snapshot_directory(
        resolved_workspace_root,
        None,
        ".",
    )
    try:
        for part in relative_parent.parts:
            child_descriptor = _open_generated_file_snapshot_directory(
                part,
                current_descriptor,
                relative_label,
            )
            os.close(current_descriptor)
            current_descriptor = child_descriptor
    except BaseException:
        os.close(current_descriptor)
        raise
    return current_descriptor


def _open_generated_file_snapshot_directory(
    target: str | Path,
    directory_descriptor: int | None,
    relative_label: str,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(target, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise RuntimeError(
            "Generated-file snapshot source directory changed before copying: "
            f"{relative_label}"
        ) from exc
    try:
        opened_stat = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    if not stat.S_ISDIR(opened_stat.st_mode):
        os.close(descriptor)
        raise RuntimeError(
            "Generated-file snapshot source directory is not a directory: "
            f"{relative_label}"
        )
    return descriptor


def _open_generated_file_snapshot_source(
    parent_descriptor: int,
    candidate: GeneratedFileSnapshotCandidate,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(
            candidate.relative_path.name,
            flags,
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise RuntimeError(
            "Generated-file snapshot source changed before copying: "
            f"{candidate.relative_label}"
        ) from exc
    try:
        _validate_generated_file_snapshot_source(os.fstat(descriptor), candidate)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _validate_generated_file_snapshot_source(
    opened_stat: os.stat_result,
    candidate: GeneratedFileSnapshotCandidate,
) -> None:
    if not stat.S_ISREG(opened_stat.st_mode):
        raise RuntimeError(
            "Generated-file snapshot source is not a regular file: "
            f"{candidate.relative_label}"
        )
    if opened_stat.st_nlink != 1:
        raise RuntimeError(
            "Generated-file snapshot source has multiple hard links: "
            f"{candidate.relative_label}"
        )
    if (opened_stat.st_dev, opened_stat.st_ino) != (
        candidate.source_device,
        candidate.source_inode,
    ):
        raise RuntimeError(
            "Generated-file snapshot source changed identity before copying: "
            f"{candidate.relative_label}"
        )
    if opened_stat.st_size != candidate.size_bytes:
        raise RuntimeError(
            "Generated-file snapshot source changed size before copying: "
            f"{candidate.relative_label}"
        )


def _copy_open_generated_file_snapshot_candidate(
    source_descriptor: int,
    candidate: GeneratedFileSnapshotCandidate,
    target: Path,
) -> tuple[int, str]:
    with target.open("wb") as target_handle:
        return copy_snapshot_bytes(source_descriptor, candidate, target_handle)


def copy_generated_result(generated_file: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=".generated-file-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
        copy_regular_file(generated_file, temporary_path)
        shutil.copymode(generated_file, temporary_path)
        temporary_path.replace(target)
    except (OSError, ValueError):
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink()
        raise


def reset_directory(path: Path) -> None:
    _ensure_safe_directory(path.parent.parent)
    prepare_directory(path.parent.parent, Path(path.parent.name))
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=False)
        return
    if not stat.S_ISDIR(mode) or path_is_symlink(path):
        raise RuntimeError(
            f"Generated-file source path is not a directory: {path.as_posix()}"
        )
    shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=False)


def prepare_directory(root: Path, relative_path: Path) -> Path:
    current = root
    for part in relative_path.parts:
        if part in {"", ".", ".."}:
            raise RuntimeError("Generated-file source path is unsafe.")
        current = current / part
        if current.exists() or current.is_symlink():
            _ensure_safe_directory(current)
            continue
        try:
            current.mkdir(exist_ok=False)
        except FileExistsError:
            _ensure_safe_directory(current)
    return current


def _ensure_safe_directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True)
        return
    if path_is_symlink(path) or not stat.S_ISDIR(mode):
        raise RuntimeError(
            f"Generated-file source path is not a directory: {path.as_posix()}"
        )
