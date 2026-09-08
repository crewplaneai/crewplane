from __future__ import annotations

import hashlib
from pathlib import Path

# Byte length and SHA-256 hex digest of the hashed content.
type ContentSignature = tuple[int, str]

FILE_HASH_CHUNK_BYTES = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(FILE_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_size_and_sha256(path: Path) -> ContentSignature:
    return path.stat().st_size, sha256_file(path)
