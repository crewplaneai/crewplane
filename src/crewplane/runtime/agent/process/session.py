"""Local process ownership contract and per-invocation selection."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import BinaryIO, Protocol

from crewplane.architecture.contracts import InvocationDiagnosticSink

from .stream_capture import ProcessOutputCapture


async def wait_for_owned_task[Result](
    task: asyncio.Future[Result],
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
    """Own process startup, containment, capture transfer, and finalization policy.

    The lifecycle starts and releases the process before collecting output. On
    failure it drains the owned process tree before closing native resources.
    """

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
    ) -> asyncio.subprocess.Process:
        """Acquire process ownership, retaining partial startup for failure drain."""
        ...

    def release(self) -> None:
        """Permit execution after ownership is recorded; ungated sessions do nothing."""
        ...

    async def collect(
        self,
        stdin_data: bytes | None,
        log_handle: BinaryIO | None,
        diagnostics: InvocationDiagnosticSink | None,
        idle_timeout: float | None,
    ) -> ProcessOutputCapture:
        """Collect output and transfer successful capture-file ownership to the caller."""
        ...

    async def drain_failed(self, diagnostics: InvocationDiagnosticSink | None) -> None:
        """Drain owned processes and pipes after failure, reporting cleanup errors."""
        ...

    def close(self) -> None:
        """Close native ownership resources; closure alone does not confirm drainage."""
        ...

    def can_report_completion(self, cleanup_confirmed: bool) -> bool:
        """Decide whether containment and drainage evidence allow completion reporting."""
        ...

    def prepare_finalization(
        self, active_exception: BaseException | None, cleanup_confirmed: bool
    ) -> None:
        """Attach platform-specific unconfirmed-cleanup evidence before reporting."""
        ...

    def finalization_requires_capture_cleanup(
        self, active_exception: BaseException | None
    ) -> bool:
        """Decide whether a finalization failure requires discarding captured output."""
        ...

    def handle_finalization_error(
        self, error: Exception, active_exception: BaseException | None
    ) -> None:
        """Raise without an active error, otherwise apply the platform error policy.

        POSIX preserves the active error. Windows job ownership promotes cleanup
        failure unless cancellation is active, in which case it attaches the cause.
        """
        ...


def process_session() -> ProcessSession:
    if sys.platform == "win32":
        from .windows_launch import WindowsLaunch

        return WindowsLaunch()
    from .posix_session import PosixProcessSession

    return PosixProcessSession()
