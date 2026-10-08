from __future__ import annotations

import hashlib
import os
from pathlib import Path

from crewplane.architecture.safe_file_reads import (
    bounded_file_chunks,
    open_regular_file,
    stable_file_signature,
)

# Byte length and SHA-256 hex digest of the hashed content.
type ContentSignature = tuple[int, str]

FILE_HASH_CHUNK_BYTES = 1024 * 1024


def sha256_file(path: Path) -> str:
    if os.name == "nt":
        return _protected_file_signature(path)[1]
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(FILE_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_size_and_sha256(path: Path) -> ContentSignature:
    if os.name == "nt":
        return _protected_file_signature(path)
    return path.stat().st_size, sha256_file(path)


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
