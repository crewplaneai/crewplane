from __future__ import annotations

from dataclasses import replace

from ..common import (
    ExecutionTelemetry,
    ProviderCallRequest,
    run_provider_call,
)
from .drift_detection import (
    capture_drift_monitoring_window,
    detect_provider_call_drift,
)
from .drift_events import emit_artifact_drift
from .types import (
    DriftGuardCallRequest,
    DriftGuardSession,
    EventLogAppendCapture,
)


def create_drift_guard_session(
    telemetry: ExecutionTelemetry | None,
) -> DriftGuardSession:
    if telemetry is None:
        return DriftGuardSession(telemetry=None, event_log_capture=None)
    capture = EventLogAppendCapture(event_sink=telemetry.event_sink, events=[])
    return DriftGuardSession(
        telemetry=replace(telemetry, event_sink=capture.emit),
        event_log_capture=capture,
    )


async def run_provider_call_with_drift_guard(
    request: DriftGuardCallRequest,
) -> int:
    monitoring_window = capture_drift_monitoring_window(
        node_id=request.node.id,
        node_dir=request.node_dir,
        output=request.output,
        telemetry=request.telemetry,
    )
    captured_telemetry, event_log_capture, event_log_start_index = (
        drift_guard_telemetry_context(request)
    )
    provider_error: Exception | None = None
    try:
        await invoke_provider_under_drift_guard(request, captured_telemetry)
    except Exception as exc:
        provider_error = exc

    try:
        drift = detect_provider_call_drift(
            request,
            monitoring_window,
            event_log_capture,
            event_log_start_index,
        )
    except Exception as drift_exc:
        if provider_error is not None:
            provider_error.add_note(f"artifact drift detection failed: {drift_exc}")
            raise provider_error from drift_exc
        raise

    try:
        emit_artifact_drift(
            telemetry=request.telemetry,
            output=request.output,
            node_id=request.node.id,
            task_id=request.task_id,
            provider=request.provider,
            role_label=request.role_label,
            audit_round_num=request.audit_round_num,
            round_num=request.round_num,
            drift=drift,
        )
    except Exception as drift_emit_exc:
        if provider_error is not None:
            provider_error.add_note(
                f"artifact drift telemetry failed: {drift_emit_exc}"
            )
            raise provider_error from drift_emit_exc
        raise

    if provider_error is not None:
        if drift.warning_paths or drift.fatal_paths:
            provider_error.add_note(
                "artifact drift detected after provider failure: "
                f"{len(drift.warning_paths)} warning path(s), "
                f"{len(drift.fatal_paths)} fatal path(s)"
            )
        raise provider_error
    if drift.fatal_paths:
        raise RuntimeError(
            f"Invocation for node '{request.node.id}' task "
            f"'{request.task_id}' modified fatal artifacts."
        )
    return 1 if drift.warning_paths else 0


def drift_guard_telemetry_context(
    request: DriftGuardCallRequest,
) -> tuple[ExecutionTelemetry | None, EventLogAppendCapture | None, int]:
    session = request.drift_session
    if session is None:
        session = create_drift_guard_session(request.telemetry)
    event_log_capture = session.event_log_capture
    event_log_start_index = (
        event_log_capture.event_count() if event_log_capture is not None else 0
    )
    return session.telemetry, event_log_capture, event_log_start_index


async def invoke_provider_under_drift_guard(
    request: DriftGuardCallRequest,
    captured_telemetry: ExecutionTelemetry | None,
) -> None:
    await run_provider_call(
        ProviderCallRequest(
            runtime_context=request.runtime_context,
            output=request.output,
            node_id=request.node.id,
            provider=request.provider,
            task_id=request.task_id,
            audit_round_num=request.audit_round_num,
            round_num=request.round_num,
            prompt=request.prompt,
            output_file=request.output_file,
            role_label=request.role_label,
            invoker=request.invoker,
            telemetry=captured_telemetry,
            findings_enabled=request.findings_enabled,
            on_log_file_resolved=request.allowed_paths.add,
        ),
        display=replace(request.display, telemetry=captured_telemetry),
    )
