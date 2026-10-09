"""Platform-dependent fingerprint-key reads, permissions, and synchronization."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.safe_file_reads import read_contained_bytes


@dataclass(frozen=True)
class KeyRead:
    key: bytes
    error: str | None = None


@dataclass(frozen=True)
class FingerprintKeyOperations:
    read_key: Callable[[Path, int], KeyRead]
    permissions_invalid: Callable[[os.stat_result], bool]
    sync_parent: Callable[[Path], None]


def fingerprint_key_operations() -> FingerprintKeyOperations:
    if os.name == "nt":
        return windows_fingerprint_key_operations()
    return FingerprintKeyOperations(
        _read_posix_key,
        _posix_permissions_invalid if os.name == "posix" else _permissions_unrestricted,
        _sync_posix_parent if os.name == "posix" else _skip_parent_sync,
    )


def windows_fingerprint_key_operations() -> FingerprintKeyOperations:
    return FingerprintKeyOperations(
        _read_windows_key, _permissions_unrestricted, _skip_parent_sync
    )


def _read_posix_key(path: Path, size: int) -> KeyRead:
    del size
    return KeyRead(path.read_bytes())


def _read_windows_key(path: Path, size: int) -> KeyRead:
    try:
        return KeyRead(read_contained_bytes(path.parent, path.name, size))
    except (OSError, ValueError) as exc:
        return KeyRead(b"", str(exc))


def _posix_permissions_invalid(metadata: os.stat_result) -> bool:
    return bool(metadata.st_mode & 0o077)


def _permissions_unrestricted(metadata: os.stat_result) -> bool:
    del metadata
    return False


def _skip_parent_sync(parent: Path) -> None:
    pass


def _sync_posix_parent(parent: Path) -> None:
    try:
        descriptor = os.open(parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
