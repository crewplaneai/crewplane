"""Local process ownership contract and per-invocation selection."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import BinaryIO, Protocol

from crewplane.architecture.contracts import InvocationDiagnosticSink

from .stream_capture import ProcessOutputCapture


async def wait_for_owned_task[Result](
    task: asyncio.Task[Result],
    cancellation: asyncio.CancelledError | None = None,
) -> asyncio.CancelledError | None:
    """Await settlement without cancelling the worker or raising its error.

    Return the first cancellation, leaving the task's outcome to its owner.
    """
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    return cancellation


class ProcessSession(Protocol):
    @property
    def process(self) -> asyncio.subprocess.Process | None: ...
    @property
    def process_group_id(self) -> int | None: ...
    async def start(
        self,
        command: list[str],
        cwd: Path,
        environment: dict[str, str] | None,
        stdin_data: bytes | None = None,
    ) -> asyncio.subprocess.Process: ...
    def release(self) -> None: ...
    async def collect(
        self,
        stdin_data: bytes | None,
        log_handle: BinaryIO | None,
        diagnostics: InvocationDiagnosticSink | None,
        idle_timeout: float | None,
    ) -> ProcessOutputCapture: ...
    async def drain_failed(
        self, diagnostics: InvocationDiagnosticSink | None
    ) -> None: ...
    def close(self) -> None: ...
    def can_report_completion(self, cleanup_confirmed: bool) -> bool: ...
    def prepare_finalization(
        self, active_exception: BaseException | None, cleanup_confirmed: bool
    ) -> None: ...
    def finalization_requires_capture_cleanup(
        self, active_exception: BaseException | None
    ) -> bool: ...
    def handle_finalization_error(
        self, error: Exception, active_exception: BaseException | None
    ) -> None: ...


def process_session() -> ProcessSession:
    if sys.platform == "win32":
        from .windows_launch import WindowsLaunch

        return WindowsLaunch()
    from .posix_session import PosixProcessSession

    return PosixProcessSession()
