"""Shared generated-file byte copying and locator validation."""

from __future__ import annotations

import os
from hashlib import sha256
from typing import BinaryIO

from crewplane.architecture.safe_files import is_safe_relative_path
from crewplane.core.file_hashing import FILE_HASH_CHUNK_BYTES

from .snapshot_policy import GeneratedFileSnapshotCandidate


def copy_snapshot_bytes(
    source_descriptor: int,
    candidate: GeneratedFileSnapshotCandidate,
    target_handle: BinaryIO,
) -> tuple[int, str]:
    digest = sha256()
    bytes_read = 0
    with os.fdopen(source_descriptor, "rb", closefd=False) as source_handle:
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


def validate_generated_file_snapshot_relative_path(
    candidate: GeneratedFileSnapshotCandidate,
) -> None:
    if not is_safe_relative_path(candidate.relative_label):
        raise RuntimeError(
            f"Generated-file snapshot source path is unsafe: {candidate.relative_label}"
        )
