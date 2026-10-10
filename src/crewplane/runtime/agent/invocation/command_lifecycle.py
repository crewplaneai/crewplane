"""Own command resources, process receipts, and finalization error precedence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import BinaryIO, cast

from crewplane.architecture.contracts import (
    CommandResult,
    InvocationContext,
    InvocationDiagnosticSink,
    InvocationProcessEvent,
)

from ..process.runner import close_log_handle
from ..process.session import ProcessSession
from ..process.stream_capture import ProcessOutputCapture


@dataclass
class CommandLifecycle:
    """Resources owned until failure cleanup or successful result transfer."""

    invocation_context: InvocationContext | None
    session: ProcessSession
    output_capture: ProcessOutputCapture | None = None
    log_handle: BinaryIO | None = None
    cleanup_confirmed: bool = False

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        return self.session.process

    @property
    def process_group_id(self) -> int | None:
        return self.session.process_group_id

    @property
    def diagnostic_sink(self) -> InvocationDiagnosticSink | None:
        if self.invocation_context is None:
            return None
        return self.invocation_context.diagnostics

    def emit_started(self) -> None:
        if self.invocation_context is None:
            return
        process = cast(asyncio.subprocess.Process, self.process)
        self._emit_event(
            InvocationProcessEvent(
                attempt=self.invocation_context.attempt_num,
                pid=process.pid,
                process_group_id=self.process_group_id,
                status="started",
            )
        )

    async def finalize(self, active_exception: BaseException | None) -> None:
        """Close resources and report exit with the platform's failure policy."""
        cancellation = (
            active_exception
            if isinstance(active_exception, asyncio.CancelledError)
            else None
        )
        self.session.prepare_finalization(active_exception, self.cleanup_confirmed)
        close_task = asyncio.create_task(
            asyncio.to_thread(close_log_handle, self.log_handle)
        )
        cancellation = await wait_for_command_io(close_task, cancellation)
        try:
            try:
                close_task.result()
            finally:
                self.session.close()
            self._emit_exit()
        except Exception as exc:
            cancellation = await self._handle_finalization_failure(
                exc, active_exception, cancellation
            )
        else:
            if cancellation is not None and active_exception is None:
                cancellation = await self.cleanup_output(cancellation)
        if cancellation is not None and cancellation is not active_exception:
            raise cancellation

    async def cleanup_output(
        self, cancellation: asyncio.CancelledError | None = None
    ) -> asyncio.CancelledError | None:
        """Await capture deletion before releasing ownership or cancellation."""
        if self.output_capture is None:
            return cancellation
        task = asyncio.create_task(asyncio.to_thread(self.output_capture.cleanup))
        cancellation = await wait_for_command_io(task, cancellation)
        command_io_result(task, cancellation)
        return cancellation

    def _emit_exit(self) -> None:
        if not self.session.can_report_completion(self.cleanup_confirmed):
            return
        context, process = self.invocation_context, self.process
        if context is None or process is None or process.returncode is None:
            return
        self._emit_event(
            InvocationProcessEvent(
                attempt=context.attempt_num,
                pid=process.pid,
                process_group_id=self.process_group_id,
                status="exited",
                returncode=process.returncode,
            )
        )

    def _emit_event(self, event: InvocationProcessEvent) -> None:
        context = cast(InvocationContext, self.invocation_context)
        if context.process_event_sink is None:
            return
        try:
            context.process_event_sink(event)
        except Exception as exc:
            raise RuntimeError(
                f"Provider process {event.status} reporting failed: {exc}"
            ) from exc

    async def _handle_finalization_failure(
        self,
        error: Exception,
        active_exception: BaseException | None,
        cancellation: asyncio.CancelledError | None,
    ) -> asyncio.CancelledError | None:
        if self.session.finalization_requires_capture_cleanup(active_exception):
            cancellation = await self.cleanup_output(cancellation)
        if cancellation is not None and active_exception is None:
            cancellation.__cause__ = error
        active_exception = cancellation or active_exception
        self.session.handle_finalization_error(error, active_exception)
        assert active_exception is not None
        active_exception.add_note(f"Provider process exit reporting failed: {error}")
        return cancellation

    def build_result(self) -> CommandResult:
        """Transfer capture-file ownership to the returned command result."""
        process = cast(asyncio.subprocess.Process, self.process)
        output_capture = cast(ProcessOutputCapture, self.output_capture)
        if process.returncode is None:
            raise RuntimeError("Provider process finished without a return code.")
        return CommandResult(
            returncode=process.returncode,
            stdout_text=output_capture.stdout_tail.decode(errors="replace"),
            stderr_text=output_capture.stderr_tail.decode(errors="replace"),
            stdout_path=output_capture.stdout.path,
            stderr_path=output_capture.stderr.path,
        )


async def wait_for_command_io[Result](
    task: asyncio.Task[Result],
    cancellation: asyncio.CancelledError | None = None,
) -> asyncio.CancelledError | None:
    """Wait for owned I/O without cancelling its worker or raising its error."""
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    return cancellation


def command_io_result[Result](
    task: asyncio.Task[Result], cancellation: asyncio.CancelledError | None
) -> Result:
    """Resolve finished I/O, retaining its failure as the cancellation cause."""
    try:
        return task.result()
    except Exception as exc:
        if cancellation is not None:
            raise cancellation from exc
        raise
