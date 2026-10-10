from __future__ import annotations

from pathlib import Path

from . import file_hashing_io
from .file_hashing_io import FILE_HASH_CHUNK_BYTES, ContentSignature

__all__ = [
    "ContentSignature",
    "FILE_HASH_CHUNK_BYTES",
    "sha256_file",
    "file_size_and_sha256",
]


def sha256_file(path: Path) -> str:
    return file_hashing_io.file_hashing_operations().sha256_file(path)


def file_size_and_sha256(path: Path) -> ContentSignature:
    return file_hashing_io.file_hashing_operations().file_signature(path)
