"""Protected, bounded reads shared by artifact consumers."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path

from . import safe_file_operations
from .safe_file_paths import is_safe_relative_path


def open_regular_file(path: Path) -> AbstractContextManager[int]:
    return safe_file_operations.safe_file_operations().open_regular_file(path)


def stable_file_signature(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    # Windows stat and fstat expose different legacy ctime semantics.
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        safe_file_operations.safe_file_operations().signature_timestamp(metadata),
        metadata.st_nlink,
    )


def bounded_file_chunks(descriptor: int) -> Iterator[bytes]:
    remaining = os.fstat(descriptor).st_size
    while chunk := os.read(descriptor, min(1024 * 1024, remaining + 1)):
        remaining -= len(chunk)
        if remaining < 0:
            raise ValueError("Source grew while reading protected file bytes.")
        yield chunk
    if remaining:
        raise ValueError("Source was truncated while reading protected file bytes.")


def copy_regular_file(source: Path, destination: Path) -> None:
    with (
        open_regular_file(source) as descriptor,
        safe_file_operations.safe_file_operations().open_destination(
            destination
        ) as target,
    ):
        initial = stable_file_signature(os.fstat(descriptor))
        for chunk in bounded_file_chunks(descriptor):
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(target, remaining)
                if written == 0:
                    raise OSError(f"Copy made no write progress: {destination}")
                remaining = remaining[written:]
        if initial != stable_file_signature(
            os.fstat(descriptor)
        ) or initial != stable_file_signature(source.lstat()):
            raise ValueError(f"Source changed during copy: {source}")


def read_contained_bytes(
    root: Path, relative_path: str, max_bytes: int | None = None
) -> bytes:
    if not is_safe_relative_path(relative_path):
        raise ValueError("Contained paths must be safe relative POSIX paths.")
    path = root.joinpath(*relative_path.split("/"))
    with safe_file_operations.safe_file_operations().open_contained_file(
        root, relative_path
    ) as descriptor:
        initial = os.fstat(descriptor)
        limit = (
            initial.st_size if max_bytes is None else min(max_bytes, initial.st_size)
        )
        if initial.st_size > limit:
            raise ValueError(f"File exceeds the {limit}-byte read limit: {path}")
        payload = bytearray()
        while chunk := os.read(descriptor, min(1024 * 1024, limit - len(payload) + 1)):
            payload.extend(chunk)
            if len(payload) > limit:
                raise ValueError(f"File grew beyond its read limit: {path}")
        expected = stable_file_signature(initial)
        if (
            len(payload) != initial.st_size
            or expected != stable_file_signature(os.fstat(descriptor))
            or expected != stable_file_signature(path.lstat())
        ):
            raise ValueError(f"File changed during protected read: {path}")
        return bytes(payload)
