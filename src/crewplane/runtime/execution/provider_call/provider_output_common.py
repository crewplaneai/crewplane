from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Protocol

from crewplane.architecture.safe_file_reads import (
    bounded_file_chunks,
    stable_file_signature,
)
from crewplane.architecture.safe_files import (
    is_single_link_regular_file,
)
from crewplane.core.file_hashing import ContentSignature


class BinaryWriter(Protocol):
    def write(self, payload: bytes) -> int: ...


def ensure_open_output_unchanged(
    path: Path,
    descriptor: int,
    initial_stat: os.stat_result,
    bytes_read: int,
) -> None:
    final_stat = os.fstat(descriptor)
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"Invocation output changed while being read: {path.as_posix()}"
        ) from exc
    if (
        not same_file_identity(initial_stat, final_stat)
        or not same_file_identity(final_stat, path_stat)
        or bytes_read != final_stat.st_size
    ):
        raise RuntimeError(
            f"Invocation output changed while being read: {path.as_posix()}"
        )


def hash_descriptor(descriptor: int) -> ContentSignature:
    digest = hashlib.sha256()
    size_bytes = 0
    for chunk in bounded_file_chunks(descriptor):
        size_bytes += len(chunk)
        digest.update(chunk)
    return size_bytes, digest.hexdigest()


def read_descriptor(descriptor: int) -> tuple[bytes, ContentSignature]:
    digest = hashlib.sha256()
    payload = bytearray()
    for chunk in bounded_file_chunks(descriptor):
        payload.extend(chunk)
        digest.update(chunk)
    return bytes(payload), (len(payload), digest.hexdigest())


def copy_descriptor(descriptor: int, destination: BinaryWriter) -> ContentSignature:
    digest = hashlib.sha256()
    size_bytes = 0
    for chunk in bounded_file_chunks(descriptor):
        destination.write(chunk)
        size_bytes += len(chunk)
        digest.update(chunk)
    return size_bytes, digest.hexdigest()


def same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return is_single_link_regular_file(second) and stable_file_signature(
        first
    ) == stable_file_signature(second)


def same_directory_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(second.st_mode)
        and not stat.S_ISLNK(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
    )


def real_directory_stat(path: Path) -> os.stat_result:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"Invocation output destination is unavailable: {path.as_posix()}"
        ) from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise RuntimeError(
            f"Invocation output destination must be a real directory: {path.as_posix()}"
        )
    return path_stat
