"""Coordinate gated asyncio startup with a private Windows process tree."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import BinaryIO, cast

from crewplane.architecture.contracts import InvocationDiagnosticSink

from . import runner, streams
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
    """Own a gated helper and its private Job Object until explicit closure.

    Start records containment before release permits provider execution. Callers
    must drain after startup or collection failure, then close the job handle.
    Closing alone does not confirm that the process tree has stopped.
    """

    def __init__(self) -> None:
        """Install ownership before startup acquires any native resources."""
        self.job: WindowsJob | None = None
        self.process_group_id: int | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.assigned = False
        self.released = False
        self.cleanup_confirmed = False

    async def start(
        self,
        command: list[str],
        cwd: Path,
        environment: dict[str, str] | None,
        stdin_data: bytes | None = None,
    ) -> asyncio.subprocess.Process:
        """Spawn a gated helper and assign it before returning.

        Cancellation waits for spawning to finish and retains a created helper
        in process for caller-owned draining without assigning or releasing it.
        Spawn failures take precedence over deferred cancellation.
        """
        del stdin_data
        self.job = WindowsJob()
        task = _spawn_helper(command, cwd, environment)
        cancellation = await _wait_for_owned_task(task)
        self.process = task.result()
        if cancellation is not None:
            raise cancellation
        self.job.assign(self.process.pid)
        self.assigned = True
        return self.process

    def release(self) -> None:
        """Write the gate byte once, after assignment and with stdin available.

        Missing ownership, a missing stdin pipe, or repeated release raises
        RuntimeError. The released flag changes only after the write succeeds.
        """
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
        """Capture output and confirm tree drainage before transferring ownership.

        The caller owns the returned capture files. On failure or cancellation,
        completed captures are reclaimed off the event loop after bounded task
        settlement. Repeated cancellation waits for owned cleanup to finish.
        """
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
        failure: BaseException | None = None
        try:
            result = await _wait_for_collection(monitor, output)
            self.cleanup_confirmed = True
        except BaseException as exc:
            failure = exc
            raise
        finally:
            await _finish_collection(monitor, output, failure)
        return result

    async def _drain_after_leader_exit(self) -> None:
        if self.process is None:
            return
        await wait_for_process_exit(self.process)
        await self.drain()

    async def drain(self) -> None:
        """Close stdin and stop the owned tree within two finite cleanup phases.

        An unassigned helper is killed directly without releasing its gate.
        Confirmation requires both helper exit and empty job membership; failed
        termination or membership queries raise ProcessDrainError with evidence.
        This does not close the Job Object or reclaim capture files.
        """
        process = self.process
        if process is None:
            self.cleanup_confirmed = True
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            await self._drain_process_tree(process)
        except (OSError, ProcessLookupError) as exc:
            raise self.cleanup_error(str(exc)) from exc

    async def _drain_process_tree(self, process: asyncio.subprocess.Process) -> None:
        for timeout in (
            PROCESS_GROUP_TERM_GRACE_SECONDS,
            PROCESS_GROUP_KILL_GRACE_SECONDS,
        ):
            self._terminate_process_tree(process)
            if await self._wait_for_drain(process, timeout):
                self.cleanup_confirmed = True
                return
        raise self.cleanup_error(
            "Windows provider Job Object did not become empty within the finite cleanup deadline."
        )

    def _terminate_process_tree(self, process: asyncio.subprocess.Process) -> None:
        job = cast(WindowsJob, self.job)
        if job.active_process_count():
            job.terminate()
        if not self.assigned and process.returncode is None:
            process.kill()

    async def _wait_for_drain(
        self, process: asyncio.subprocess.Process, timeout: float
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if (
                process.returncode is not None
                and cast(WindowsJob, self.job).active_process_count() == 0
            ):
                return True
            await asyncio.sleep(0.01)
        return False

    def cleanup_error(self, reason: str) -> ProcessDrainError:
        """Build unconfirmed tree-cleanup evidence from the current helper state."""
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
        """Close the kill-on-close job handle without confirming tree drainage.

        Call after drain, including failed drain attempts. Native close errors
        propagate; capture-file ownership is unaffected.
        """
        if self.job is not None:
            self.job.close()

    async def drain_failed(self, diagnostics: InvocationDiagnosticSink | None) -> None:
        await self.drain()
        if self.process is not None:
            await runner.reap_failed_process(self.process, None, diagnostics)
            await streams.drain_process_pipes(self.process, diagnostics, None)

    def can_report_completion(self, cleanup_confirmed: bool) -> bool:
        return cleanup_confirmed and self.assigned

    def prepare_finalization(
        self, active_exception: BaseException | None, cleanup_confirmed: bool
    ) -> None:
        if self.job is not None and not cleanup_confirmed:
            cleanup_error = self.cleanup_error(
                "Windows provider cleanup was interrupted before confirmation."
            )
            if active_exception is not None:
                active_exception.__cause__ = cleanup_error

    def finalization_requires_capture_cleanup(
        self, active_exception: BaseException | None
    ) -> bool:
        return self.job is not None or active_exception is None

    def handle_finalization_error(
        self, error: Exception, active_exception: BaseException | None
    ) -> None:
        if self.job is not None:
            cleanup_error = self.cleanup_error(
                f"Provider cleanup reporting failed: {error}"
            )
            if not isinstance(active_exception, asyncio.CancelledError):
                raise cleanup_error from error
            active_exception.__cause__ = cleanup_error
        if active_exception is None:
            raise error


def _spawn_helper(
    command: list[str], cwd: Path, environment: dict[str, str] | None
) -> asyncio.Task[asyncio.subprocess.Process]:
    return asyncio.create_task(
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


async def _wait_for_owned_task[Result](
    task: asyncio.Task[Result], cancellation: asyncio.CancelledError | None = None
) -> asyncio.CancelledError | None:
    """Defer cancellation until the owned task finishes, leaving errors to result()."""
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    return cancellation


async def _wait_for_collection(
    monitor: asyncio.Task[None], output: asyncio.Task[ProcessOutputCapture]
) -> ProcessOutputCapture:
    done, _pending = await asyncio.wait(
        (monitor, output), return_when=asyncio.FIRST_COMPLETED
    )
    if monitor in done:
        await monitor
    result = await output
    await monitor
    return result


async def _finish_collection(
    monitor: asyncio.Task[None],
    output: asyncio.Task[ProcessOutputCapture],
    failure: BaseException | None,
) -> None:
    cancellation = failure if isinstance(failure, asyncio.CancelledError) else None
    settlement = asyncio.create_task(_settle_collection_tasks(monitor, output))
    cancellation = await _wait_for_owned_task(settlement, cancellation)
    try:
        settlement.result()
    except BaseException as exc:
        cancellation = await _reclaim_capture(output, cancellation)
        if cancellation is not None:
            raise cancellation from exc
        raise
    if failure is not None or cancellation is not None:
        cancellation = await _reclaim_capture(output, cancellation)
    if cancellation is not None and cancellation is not failure:
        raise cancellation


async def _settle_collection_tasks(
    monitor: asyncio.Task[None], output: asyncio.Task[ProcessOutputCapture]
) -> None:
    await cancel_pending_stream_tasks(monitor, output)
    await asyncio.gather(monitor, output, return_exceptions=True)


async def _reclaim_capture(
    output: asyncio.Task[ProcessOutputCapture],
    cancellation: asyncio.CancelledError | None,
) -> asyncio.CancelledError | None:
    if not output.done() or output.cancelled() or output.exception() is not None:
        return cancellation
    cleanup = asyncio.create_task(asyncio.to_thread(output.result().cleanup))
    cancellation = await _wait_for_owned_task(cleanup, cancellation)
    cleanup.result()
    return cancellation
