"""Protected, bounded reads shared by artifact consumers."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .safe_files import (
    contained_regular_file,
    is_safe_relative_path,
    is_single_link_regular_file,
)


@contextmanager
def open_regular_file(path: Path) -> Iterator[int]:
    if os.name == "nt":
        from .safe_files_windows import open_regular_file as open_windows_file

        with open_windows_file(path) as descriptor:
            yield descriptor
        return
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(path, flags)
    try:
        if not is_single_link_regular_file(os.fstat(descriptor)):
            raise ValueError(
                f"Protected source must be a single-link regular file: {path}"
            )
        yield descriptor
    finally:
        os.close(descriptor)


def stable_file_signature(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    # Windows stat and fstat expose different legacy ctime semantics.
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        int(getattr(metadata, "st_birthtime_ns"))  # noqa: B009 - Windows-only stat field.
        if os.name == "nt"
        else metadata.st_ctime_ns,
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


@contextmanager
def _open_destination(path: Path) -> Iterator[int]:
    if os.name == "nt":
        from .safe_files_windows import open_writable_file

        with open_writable_file(path) as descriptor:
            yield descriptor
        return
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not is_single_link_regular_file(os.fstat(descriptor)):
            raise ValueError(
                f"Copy destination is not a single-link regular file: {path}"
            )
        os.ftruncate(descriptor, 0)
        yield descriptor
    finally:
        os.close(descriptor)


def copy_regular_file(source: Path, destination: Path) -> None:
    with (
        open_regular_file(source) as descriptor,
        _open_destination(destination) as target,
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
    if os.name != "nt" and contained_regular_file(root, relative_path) is None:
        path.lstat()
        raise ValueError(f"Contained source is missing or unsafe: {path}")
    with open_regular_file(path) as descriptor:
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
