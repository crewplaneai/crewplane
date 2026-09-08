from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import BinaryIO

from crewplane.core.file_hashing import ContentSignature

_RECOVERY_COPY_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class RuntimeEventPublication:
    """Durable event-log bytes published by one runtime invocation."""

    owner_id: str
    line: bytes


@dataclass(frozen=True)
class _RecoverySlice:
    offset: int
    size: int


@dataclass
class RuntimePublicationRegistry:
    """Track runtime publications and disk-backed recovery snapshots."""

    _published_signatures: dict[Path, ContentSignature] = field(
        default_factory=dict,
        repr=False,
    )
    _recovery_slices: dict[Path, _RecoverySlice] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _recovery_signatures: dict[Path, ContentSignature] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _recovery_spool: BinaryIO | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _event_publications: list[RuntimeEventPublication] = field(
        default_factory=list,
        repr=False,
    )
    _publication_owners: dict[Path, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _current_publication_owner: ContextVar[str | None] = field(
        default_factory=lambda: ContextVar(
            "crewplane_runtime_publication_owner",
            default=None,
        ),
        repr=False,
        compare=False,
    )
    _lock: RLock = field(default_factory=RLock, repr=False, compare=False)
    _version: int = field(default=0, repr=False, compare=False)
    _event_publication_depth: int = field(default=0, repr=False, compare=False)
    _closed: bool = field(default=False, repr=False, compare=False)

    def publish(
        self,
        path: Path,
        signature: ContentSignature,
        recovery_source: Path | None = None,
    ) -> None:
        with self._lock:
            self._ensure_open()
            if recovery_source is None:
                self._recovery_signatures.pop(path, None)
                self._recovery_slices.pop(path, None)
            else:
                recovery_slice = _append_recovery_source(
                    self._ensure_recovery_spool(),
                    recovery_source,
                    signature,
                )
                self._recovery_signatures[path] = signature
                self._recovery_slices[path] = recovery_slice
            self._published_signatures[path] = signature
            owner_id = self._current_publication_owner.get()
            if owner_id is None:
                self._publication_owners.pop(path, None)
            else:
                self._publication_owners[path] = owner_id
            self._version += 1

    def capture_recovery_snapshot(
        self,
        path: Path,
        signature: ContentSignature,
    ) -> None:
        """Retain stable recovery bytes without registering a new publication."""

        with self._lock:
            self._ensure_open()
            self._capture_recovery_snapshot(path, signature, path)

    def copy_recovery_payload_to(self, path: Path, destination: BinaryIO) -> bool:
        """Copy exact restorable bytes to ``destination`` without materializing them."""

        with self._lock:
            self._ensure_open()
            recovery_slice = self._recovery_slices.get(path)
            if recovery_slice is None:
                return False
            recovery_spool = self._require_recovery_spool()
            expected_signature = self._recovery_signatures[path]
            recovery_spool.seek(recovery_slice.offset)
            digest = hashlib.sha256()
            size_bytes = 0
            remaining_bytes = recovery_slice.size
            while remaining_bytes:
                chunk = recovery_spool.read(
                    min(remaining_bytes, _RECOVERY_COPY_CHUNK_BYTES)
                )
                if not chunk:
                    raise RuntimeError(
                        "Runtime publication recovery snapshot ended unexpectedly: "
                        f"{path.as_posix()}"
                    )
                written = destination.write(chunk)
                if written != len(chunk):
                    raise OSError(
                        "Runtime publication recovery destination accepted a "
                        "partial write."
                    )
                digest.update(chunk)
                size_bytes += len(chunk)
                remaining_bytes -= len(chunk)
            if (size_bytes, digest.hexdigest()) != expected_signature:
                raise RuntimeError(
                    "Runtime publication recovery snapshot no longer matches its "
                    f"registered signature: {path.as_posix()}"
                )
            return True

    def close(self) -> None:
        """Release all private recovery files after workflow execution."""

        with self._lock:
            if self._closed:
                return
            recovery_spool = self._recovery_spool
            self._recovery_spool = None
            self._recovery_slices.clear()
            self._recovery_signatures.clear()
            self._publication_owners.clear()
            self._closed = True
        if recovery_spool is not None:
            recovery_spool.close()

    def __del__(self) -> None:
        with suppress(AttributeError, OSError):
            self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Hide a runtime-owned write until its signature is registered."""

        with self._lock:
            self._ensure_open()
            yield

    @contextmanager
    def attribute_to(self, owner_id: str) -> Iterator[None]:
        """Attribute publications in the current execution context to one owner."""

        with self._lock:
            self._ensure_open()
        token = self._current_publication_owner.set(owner_id)
        try:
            yield
        finally:
            self._current_publication_owner.reset(token)

    @contextmanager
    def event_publication(
        self,
        owner_id: str,
        line: bytes,
    ) -> Iterator[None]:
        """Serialize an event-log write with its exact runtime attribution."""

        with self._lock:
            self._ensure_open()
            is_outermost = self._event_publication_depth == 0
            self._event_publication_depth += 1
            try:
                yield
            except BaseException:
                raise
            else:
                if is_outermost:
                    self._event_publications.append(
                        RuntimeEventPublication(owner_id=owner_id, line=line)
                    )
                    self._version += 1
            finally:
                self._event_publication_depth -= 1

    @contextmanager
    def event_observation(self) -> Iterator[None]:
        """Block attributed event publication while a log boundary is observed."""

        with self._lock:
            self._ensure_open()
            yield

    def event_publication_cursor(self) -> int:
        with self._lock:
            return len(self._event_publications)

    def event_publications_since(
        self,
        cursor: int,
    ) -> tuple[RuntimeEventPublication, ...]:
        with self._lock:
            if cursor < 0 or cursor > len(self._event_publications):
                raise ValueError("Runtime event publication cursor is invalid.")
            return tuple(self._event_publications[cursor:])

    def snapshot(self) -> tuple[dict[Path, ContentSignature], int]:
        with self._lock:
            return dict(self._published_signatures), self._version

    def snapshot_with_owners(
        self,
    ) -> tuple[dict[Path, ContentSignature], dict[Path, str], int]:
        with self._lock:
            return (
                dict(self._published_signatures),
                dict(self._publication_owners),
                self._version,
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Runtime publication registry is closed.")

    def _capture_recovery_snapshot(
        self,
        path: Path,
        signature: ContentSignature,
        source: Path,
    ) -> None:
        if (
            self._recovery_signatures.get(path) == signature
            and path in self._recovery_slices
        ):
            return
        recovery_slice = _append_recovery_source(
            self._ensure_recovery_spool(),
            source,
            signature,
        )
        self._recovery_signatures[path] = signature
        self._recovery_slices[path] = recovery_slice

    def _ensure_recovery_spool(self) -> BinaryIO:
        if self._recovery_spool is None:
            self._recovery_spool = tempfile.TemporaryFile(  # noqa: SIM115
                mode="w+b"
            )
        return self._recovery_spool

    def _require_recovery_spool(self) -> BinaryIO:
        if self._recovery_spool is None:
            raise RuntimeError("Runtime publication recovery spool is unavailable.")
        return self._recovery_spool


def _append_recovery_source(
    recovery_spool: BinaryIO,
    source: Path,
    expected_signature: ContentSignature,
) -> _RecoverySlice:
    start_offset = recovery_spool.seek(0, os.SEEK_END)
    try:
        digest = hashlib.sha256()
        size_bytes = 0
        with source.open("rb") as source_file:
            source_stat = os.fstat(source_file.fileno())
            if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
                raise RuntimeError(
                    "Runtime publication recovery source must be a single-link "
                    f"regular file: {source.as_posix()}"
                )
            while chunk := source_file.read(_RECOVERY_COPY_CHUNK_BYTES):
                written = recovery_spool.write(chunk)
                if written != len(chunk):
                    raise OSError(
                        "Runtime publication recovery spool accepted a partial write."
                    )
                digest.update(chunk)
                size_bytes += len(chunk)
        if (size_bytes, digest.hexdigest()) != expected_signature:
            raise ValueError(
                "Runtime publication recovery source does not match its signature."
            )
        recovery_spool.flush()
        return _RecoverySlice(offset=start_offset, size=size_bytes)
    except BaseException:
        recovery_spool.seek(start_offset)
        recovery_spool.truncate()
        recovery_spool.flush()
        raise
