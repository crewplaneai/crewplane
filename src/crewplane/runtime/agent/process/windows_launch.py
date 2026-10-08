"""Coordinate gated asyncio startup with a private Windows process tree."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import BinaryIO, cast

from crewplane.architecture.contracts import InvocationDiagnosticSink

from .drain import (
    PROCESS_GROUP_KILL_GRACE_SECONDS,
    PROCESS_GROUP_TERM_GRACE_SECONDS,
    ProcessDrainError,
    ProcessDrainEvidence,
)
from .signals import wait_for_process_exit
from .stream_capture import ProcessOutputCapture
from .streams import cancel_pending_stream_tasks, write_stdin_and_collect_output
from .windows_gate import GATE_BYTE
from .windows_job import WindowsJob


class WindowsLaunch:
    def __init__(self) -> None:
        self.job = WindowsJob()
        self.process: asyncio.subprocess.Process | None = None
        self.assigned = False
        self.released = False
        self.cleanup_confirmed = False

    async def start(
        self, command: list[str], cwd: Path, environment: dict[str, str] | None
    ) -> asyncio.subprocess.Process:
        task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                # A venv redirector can spawn the interpreter before job assignment.
                cast(str, vars(sys)["_base_executable"]),
                str(Path(__file__).with_name("windows_gate.py")),
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=environment,
            )
        )
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
        self.process = task.result()
        if cancellation is not None:
            raise cancellation
        self.job.assign(self.process.pid)
        self.assigned = True
        return self.process

    def release(self) -> None:
        if (
            not self.assigned
            or self.released
            or self.process is None
            or self.process.stdin is None
        ):
            raise RuntimeError(
                "Windows provider gate cannot be released without recorded ownership."
            )
        self.process.stdin.write(GATE_BYTE)
        self.released = True

    async def collect(
        self,
        stdin_data: bytes | None,
        log_handle: BinaryIO | None,
        diagnostics: InvocationDiagnosticSink | None,
        idle_timeout: float | None,
    ) -> ProcessOutputCapture:
        if self.process is None:
            raise RuntimeError("Windows provider helper has not started.")
        monitor = asyncio.create_task(self._drain_after_leader_exit())
        output = asyncio.create_task(
            write_stdin_and_collect_output(
                self.process,
                stdin_data or b"",
                log_handle,
                diagnostics,
                None,
                idle_timeout,
            )
        )
        completed = False
        try:
            done, _pending = await asyncio.wait(
                (monitor, output), return_when=asyncio.FIRST_COMPLETED
            )
            if monitor in done:
                await monitor
            result = await output
            await monitor
            self.cleanup_confirmed = True
            completed = True
            return result
        finally:
            await cancel_pending_stream_tasks(monitor, output)
            outcomes = await asyncio.gather(monitor, output, return_exceptions=True)
            if not completed:
                for outcome in outcomes:
                    if isinstance(outcome, ProcessOutputCapture):
                        outcome.cleanup()

    async def _drain_after_leader_exit(self) -> None:
        if self.process is None:
            return
        await wait_for_process_exit(self.process)
        await self.drain()

    async def drain(self) -> None:
        process = self.process
        if process is None:
            self.cleanup_confirmed = True
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            for timeout in (
                PROCESS_GROUP_TERM_GRACE_SECONDS,
                PROCESS_GROUP_KILL_GRACE_SECONDS,
            ):
                if self.job.active_process_count():
                    self.job.terminate()
                if not self.assigned and process.returncode is None:
                    process.kill()
                deadline = asyncio.get_running_loop().time() + timeout
                while asyncio.get_running_loop().time() < deadline:
                    if (
                        process.returncode is not None
                        and self.job.active_process_count() == 0
                    ):
                        self.cleanup_confirmed = True
                        return
                    await asyncio.sleep(0.01)
        except (OSError, ProcessLookupError) as exc:
            raise self.cleanup_error(str(exc)) from exc
        raise self.cleanup_error(
            "Windows provider Job Object did not become empty within the finite cleanup deadline."
        )

    def cleanup_error(self, reason: str) -> ProcessDrainError:
        return ProcessDrainError(
            ProcessDrainEvidence(
                self.process.pid if self.process else -1,
                None,
                self.process is None or self.process.returncode is not None,
                False,
            ),
            reason,
        )

    def close(self) -> None:
        self.job.close()
