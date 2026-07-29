from __future__ import annotations

from dataclasses import replace
from typing import Never

from ..common import (
    ExecutionTelemetry,
    ProviderCallRequest,
    run_provider_call,
)
from ..errors import NodeExecutionError, is_expected_execution_failure
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

    allow_runtime_generated_file_snapshots(request)

    mixed_error: Exception | None = None
    try:
        drift = detect_provider_call_drift(
            request,
            monitoring_window,
            event_log_capture,
            event_log_start_index,
        )
    except Exception as drift_exc:
        if provider_error is not None:
            mixed_error = mixed_provider_and_drift_guard_error(
                provider_error,
                drift_exc,
                "artifact drift detection failed",
            )
        else:
            raise
    if mixed_error is not None:
        raise_preserving_cause(mixed_error)

    mixed_error = None
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
            mixed_error = mixed_provider_and_drift_guard_error(
                provider_error,
                drift_emit_exc,
                "artifact drift telemetry failed",
            )
        else:
            raise
    if mixed_error is not None:
        raise_preserving_cause(mixed_error)

    if provider_error is not None:
        if drift.warning_paths or drift.fatal_paths:
            provider_error.add_note(
                "artifact drift detected after provider failure: "
                f"{len(drift.warning_paths)} warning path(s), "
                f"{len(drift.fatal_paths)} fatal path(s)"
            )
        if drift.fatal_paths:
            if not is_expected_execution_failure(provider_error):
                raise provider_error
            raise fatal_artifact_drift_error(request) from provider_error
        raise provider_error
    if drift.fatal_paths:
        raise fatal_artifact_drift_error(request)
    return 1 if drift.warning_paths else 0


def mixed_provider_and_drift_guard_error(
    provider_error: Exception,
    drift_guard_error: Exception,
    context: str,
) -> Exception:
    if is_expected_execution_failure(provider_error):
        drift_guard_error.add_note(f"provider call failed first: {provider_error}")
        return drift_guard_error
    provider_error.add_note(f"{context}: {drift_guard_error}")
    return provider_error


def raise_preserving_cause(exc: Exception) -> Never:
    if exc.__cause__ is not None:
        raise exc from exc.__cause__
    raise exc


def fatal_artifact_drift_error(request: DriftGuardCallRequest) -> NodeExecutionError:
    return NodeExecutionError(
        f"Invocation for node '{request.node.id}' task "
        f"'{request.task_id}' modified fatal artifacts."
    )


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
            provider_output_policy=request.provider_output_policy,
            on_log_file_resolved=request.allowed_paths.add,
            rendered_workspace_files=request.rendered_workspace_files,
        ),
        display=replace(request.display, telemetry=captured_telemetry),
    )


def allow_runtime_generated_file_snapshots(request: DriftGuardCallRequest) -> None:
    snapshot_root = request.runtime_context.generated_file_workspaces.roots_for_node(
        request.node.id
    ).get(request.output_file.resolve(strict=False))
    if snapshot_root is None:
        return
    if not snapshot_root.exists():
        return
    request.allowed_paths.update(
        path for path in snapshot_root.rglob("*") if path.is_file()
    )
