from __future__ import annotations

import os
import tempfile
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
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
        if max_memory_bytes < 0:
            raise ValueError("max_memory_bytes must be nonnegative")
        descriptor, raw_path = tempfile.mkstemp()
        handle: BinaryIO | None = None
        try:
            self._path = Path(raw_path)
            handle = os.fdopen(descriptor, "wb")
            self._persist_handle = handle
            self._tail_chunks: deque[bytes] = deque()
            self._tail_size = 0
            self._max_memory_bytes = max_memory_bytes
            self._closed = False
        except BaseException as exc:
            try:
                if handle is None:
                    os.close(descriptor)
                else:
                    handle.close()
            except BaseException as cleanup_error:
                exc.add_note(f"Capture descriptor cleanup failed: {cleanup_error!r}")
            try:
                os.unlink(raw_path)
            except OSError as cleanup_error:
                exc.add_note(f"Capture file cleanup failed: {cleanup_error!r}")
            raise

    def write(self, payload: bytes) -> None:
        self._persist_handle.write(payload)
        if not payload or self._max_memory_bytes == 0:
            return
        if len(payload) >= self._max_memory_bytes:
            self._tail_chunks.clear()
            self._tail_chunks.append(payload[-self._max_memory_bytes :])
            self._tail_size = self._max_memory_bytes
            return
        self._tail_chunks.append(payload)
        self._tail_size += len(payload)
        self._discard_tail_overflow()

    def _discard_tail_overflow(self) -> None:
        overflow = self._tail_size - self._max_memory_bytes
        while overflow > 0:
            first = self._tail_chunks[0]
            if len(first) <= overflow:
                self._tail_chunks.popleft()
                self._tail_size -= len(first)
                overflow -= len(first)
                continue
            self._tail_chunks[0] = first[overflow:]
            self._tail_size -= overflow
            overflow = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def tail_bytes(self) -> bytes:
        return b"".join(self._tail_chunks)

    def close(self) -> None:
        if self._closed:
            return
        failure: BaseException | None = None
        try:
            self._persist_handle.flush()
        except BaseException as exc:
            failure = exc
        try:
            self._persist_handle.close()
            self._closed = True
        except BaseException as exc:
            if failure is None:
                failure = exc
            else:
                failure.add_note(f"Capture close failed: {exc!r}")
        if failure is not None:
            raise failure

    def cleanup(self) -> None:
        failure: BaseException | None = None
        try:
            self.close()
        except BaseException as exc:
            failure = exc
        try:
            self._path.unlink(missing_ok=True)
        except BaseException as exc:
            if failure is None:
                failure = exc
            else:
                failure.add_note(f"Capture unlink failed: {exc!r}")
        if failure is not None:
            raise failure


def close_captures(captures: list[CapturedStream]) -> None:
    failure: BaseException | None = None
    for capture in captures:
        try:
            capture.close()
        except BaseException as exc:
            if failure is None:
                failure = exc
            else:
                failure.add_note(f"Sibling capture close failed: {exc!r}")
    if failure is not None:
        raise failure


@dataclass(frozen=True)
class ProcessOutputCapture:
    """Runtime capture bundle with persisted output files and tail metadata."""

    stdout: ProcessStreamCapture
    stderr: ProcessStreamCapture

    def __iter__(self) -> Iterator[bytes]:
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
