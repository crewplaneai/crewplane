from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from os import close as close_descriptor
from pathlib import Path
from typing import BinaryIO

STDOUT_PREFIX = b""
STDERR_PREFIX = b"[stderr] "
MAX_CAPTURE_MEMORY_BYTES = 1_048_576


@dataclass(frozen=True)
class ProcessStreamCapture:
    """Persisted stream content with a bounded in-memory tail."""

    path: Path
    tail_bytes: bytes

    def iter_lines(self) -> Iterator[str]:
        if self.path.exists():
            with self.path.open("rb") as handle:
                for raw_line in handle:
                    line = raw_line.decode(errors="replace").rstrip("\n")
                    yield line
            return
        if self.tail_bytes:
            for line in self.tail_bytes.decode(errors="replace").splitlines():
                yield line

    def cleanup(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            return


class CapturedStream:
    """Bounded in-memory tail plus disk-backed stream file."""

    def __init__(self, max_memory_bytes: int = MAX_CAPTURE_MEMORY_BYTES) -> None:
        descriptor, raw_path = tempfile.mkstemp()
        close_descriptor(descriptor)
        self._path = Path(raw_path)
        self._persist_handle: BinaryIO = self._path.open("wb")  # noqa: SIM115
        self._tail = b""
        self._max_memory_bytes = max_memory_bytes
        self._closed = False

    def write(self, payload: bytes) -> None:
        self._persist_handle.write(payload)
        if payload:
            self._tail += payload
            overflow = len(self._tail) - self._max_memory_bytes
            if overflow > 0:
                self._tail = self._tail[overflow:]

    @property
    def path(self) -> Path:
        return self._path

    @property
    def tail_bytes(self) -> bytes:
        return self._tail

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._persist_handle.flush()
        finally:
            self._persist_handle.close()
            self._closed = True

    def cleanup(self) -> None:
        with suppress(OSError):
            self.close()
        with suppress(OSError):
            self._path.unlink()


@dataclass(frozen=True)
class ProcessOutputCapture:
    """Runtime capture bundle with persisted output files and tail metadata."""

    stdout: ProcessStreamCapture
    stderr: ProcessStreamCapture

    def __iter__(self):
        return iter((self.stdout.tail_bytes, self.stderr.tail_bytes))

    def cleanup(self) -> None:
        self.stdout.cleanup()
        self.stderr.cleanup()

    @property
    def stdout_tail(self) -> bytes:
        return self.stdout.tail_bytes

    @property
    def stderr_tail(self) -> bytes:
        return self.stderr.tail_bytes
