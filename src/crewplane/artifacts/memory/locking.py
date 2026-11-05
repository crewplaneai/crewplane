from __future__ import annotations

import asyncio
import errno
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from crewplane.architecture.safe_file_reads import open_regular_file


@asynccontextmanager
async def memory_lock(
    path: Path, exclusive: bool = True, timeout_seconds: float = 10.0
) -> AsyncIterator[None]:
    """Own one persistent lock descriptor, including while cancelled waiting."""
    if os.name == "nt":
        raise ValueError(
            "Project memory requires supported Linux, macOS, or WSL flock semantics."
        )
    import fcntl

    deadline = time.monotonic() + timeout_seconds
    with open_regular_file(path) as descriptor:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        while True:
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EAGAIN, errno.EACCES}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting for project memory lock: {path}"
                    ) from exc
                await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            current = path.lstat()
            held = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise ValueError(
                    "Project memory lock identity changed while acquiring it."
                )
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
