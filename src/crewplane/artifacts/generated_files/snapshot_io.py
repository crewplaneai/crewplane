from __future__ import annotations

import os
import stat
from hashlib import sha256
from pathlib import Path

from crewplane.core.file_hashing import FILE_HASH_CHUNK_BYTES

from .snapshot_policy import GeneratedFileSnapshotCandidate


def copy_generated_file_snapshot_candidate(
    candidate: GeneratedFileSnapshotCandidate,
    target: Path,
    resolved_workspace_root: Path,
) -> tuple[int, str]:
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
    _validate_generated_file_snapshot_relative_path(candidate)
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
        opened_stat = os.fstat(descriptor)
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
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _copy_open_generated_file_snapshot_candidate(
    source_descriptor: int,
    candidate: GeneratedFileSnapshotCandidate,
    target: Path,
) -> tuple[int, str]:
    digest = sha256()
    bytes_read = 0
    with (
        os.fdopen(source_descriptor, "rb", closefd=False) as source_handle,
        target.open("wb") as target_handle,
    ):
        for payload in iter(lambda: source_handle.read(FILE_HASH_CHUNK_BYTES), b""):
            bytes_read += len(payload)
            if bytes_read > candidate.size_bytes:
                raise RuntimeError(
                    "Generated-file snapshot source grew while copying: "
                    f"{candidate.relative_label}"
                )
            target_handle.write(payload)
            digest.update(payload)
    if bytes_read != candidate.size_bytes:
        raise RuntimeError(
            "Generated-file snapshot source changed while copying: "
            f"{candidate.relative_label}"
        )
    return bytes_read, digest.hexdigest()


def _validate_generated_file_snapshot_relative_path(
    candidate: GeneratedFileSnapshotCandidate,
) -> None:
    relative_path = candidate.relative_path
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise RuntimeError(
            f"Generated-file snapshot source path is unsafe: {candidate.relative_label}"
        )
