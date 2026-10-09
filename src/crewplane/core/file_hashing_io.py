from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.safe_file_reads import (
    bounded_file_chunks,
    open_regular_file,
    stable_file_signature,
)

# Byte length and SHA-256 hex digest of the hashed content.
type ContentSignature = tuple[int, str]

FILE_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class FileHashingOperations:
    sha256_file: Callable[[Path], str]
    file_signature: Callable[[Path], ContentSignature]


def file_hashing_operations() -> FileHashingOperations:
    if os.name == "nt":
        return windows_file_hashing_operations()
    return FileHashingOperations(_posix_sha256, _posix_file_signature)


def windows_file_hashing_operations() -> FileHashingOperations:
    return FileHashingOperations(_protected_sha256, _protected_file_signature)


def _protected_sha256(path: Path) -> str:
    return _protected_file_signature(path)[1]


def _posix_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(FILE_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _posix_file_signature(path: Path) -> ContentSignature:
    return path.stat().st_size, _posix_sha256(path)


def _protected_file_signature(path: Path) -> ContentSignature:
    with open_regular_file(path) as descriptor:
        initial = os.fstat(descriptor)
        digest = hashlib.sha256()
        size = 0
        for chunk in bounded_file_chunks(descriptor):
            digest.update(chunk)
            size += len(chunk)
        if size != initial.st_size or stable_file_signature(
            initial
        ) != stable_file_signature(os.fstat(descriptor)):
            raise ValueError(f"File changed while hashing: {path}")
        return size, digest.hexdigest()
