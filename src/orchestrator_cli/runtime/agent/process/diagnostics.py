from __future__ import annotations

from ..types import InvocationDiagnostic, InvocationDiagnosticSink

PROCESS_EXIT_WARNING_MESSAGE = "Provider process already exited before signal delivery."
PROCESS_PIPE_DRAIN_GRACE_SECONDS = 0.25


def emit_idle_timeout_diagnostic(
    diagnostic_sink: InvocationDiagnosticSink | None,
    idle_timeout_seconds: float,
) -> None:
    if diagnostic_sink is None:
        return
    diagnostic_sink(
        InvocationDiagnostic(
            level="error",
            message=(
                "Provider invocation produced no stdout or stderr during the "
                "idle timeout window."
            ),
            operation="invocation_idle_timeout",
            attributes={
                "idle_timeout_seconds": idle_timeout_seconds,
            },
        )
    )


def emit_pipe_drain_timeout_diagnostic(
    diagnostic_sink: InvocationDiagnosticSink | None,
    stdout_pending: bool,
    stderr_pending: bool,
) -> None:
    if diagnostic_sink is None:
        return
    diagnostic_sink(
        InvocationDiagnostic(
            level="warning",
            message=(
                "Provider process exited while stdio pipes remained open; "
                "continuing with captured output."
            ),
            operation="process_pipe_drain_timeout",
            attributes={
                "drain_grace_seconds": PROCESS_PIPE_DRAIN_GRACE_SECONDS,
                "stdout_pending": stdout_pending,
                "stderr_pending": stderr_pending,
            },
        )
    )


def emit_process_already_exited_diagnostic(
    diagnostic_sink: InvocationDiagnosticSink | None,
    attempted_signal: str,
) -> None:
    if diagnostic_sink is None:
        return
    diagnostic_sink(
        InvocationDiagnostic(
            level="warning",
            message=PROCESS_EXIT_WARNING_MESSAGE,
            operation="process_already_exited_before_signal",
            attributes={"attempted_signal": attempted_signal},
        )
    )
