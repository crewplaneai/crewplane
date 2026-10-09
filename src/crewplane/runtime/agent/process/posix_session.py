"""POSIX subprocess startup, process-group ownership, and failure policy."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import BinaryIO, cast

from crewplane.architecture.contracts import InvocationDiagnosticSink
from crewplane.core.platform import supports_posix_process_groups

from . import runner, streams
from .stream_capture import ProcessOutputCapture


class PosixProcessSession:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.process_group_id: int | None = None

    async def start(
        self,
        command: list[str],
        cwd: Path,
        environment: dict[str, str] | None,
        stdin_data: bytes | None = None,
    ) -> asyncio.subprocess.Process:
        start_new_session = supports_posix_process_groups()
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=environment,
            start_new_session=start_new_session,
        )
        self.process_group_id = self.process.pid if start_new_session else None
        return self.process

    def release(self) -> None:
        pass

    async def collect(
        self,
        stdin_data: bytes | None,
        log_handle: BinaryIO | None,
        diagnostics: InvocationDiagnosticSink | None,
        idle_timeout: float | None,
    ) -> ProcessOutputCapture:
        return await runner.write_stdin_and_collect_output(
            cast(asyncio.subprocess.Process, self.process),
            stdin_data,
            log_handle,
            diagnostics,
            self.process_group_id,
            idle_timeout,
        )

    async def drain_failed(self, diagnostics: InvocationDiagnosticSink | None) -> None:
        if self.process is not None:
            await runner.reap_failed_process(
                self.process, self.process_group_id, diagnostics
            )
            await streams.drain_process_pipes(
                self.process, diagnostics, self.process_group_id
            )

    def close(self) -> None:
        pass

    def can_report_completion(self, cleanup_confirmed: bool) -> bool:
        return cleanup_confirmed

    def prepare_finalization(
        self, active_exception: BaseException | None, cleanup_confirmed: bool
    ) -> None:
        pass

    def finalization_requires_capture_cleanup(
        self, active_exception: BaseException | None
    ) -> bool:
        return active_exception is None

    def handle_finalization_error(
        self, error: Exception, active_exception: BaseException | None
    ) -> None:
        if active_exception is None:
            raise error
