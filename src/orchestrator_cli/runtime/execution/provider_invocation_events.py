from __future__ import annotations

from pathlib import Path

from orchestrator_cli.architecture.contracts import (
    InvocationContext,
    InvocationDiagnostic,
)
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.observability.timing import ElapsedTimer
from orchestrator_cli.runtime.agent.usage import (
    InvocationUsage,
    build_fallback_usage_from_output_file,
)

from .execution_activity import ExecutionTelemetry
from .execution_events import (
    InvocationEventCapture,
    InvocationMetadata,
    emit_invocation_event,
    emit_runtime_log,
    failure_event_fields,
    safe_error_message,
)
from .provider_display import (
    ProviderCallDisplay,
    provider_console_message_sink,
)


def build_invocation_context(
    telemetry: ExecutionTelemetry | None,
    metadata: InvocationMetadata,
    display: ProviderCallDisplay,
) -> tuple[InvocationContext, InvocationEventCapture]:
    capture = InvocationEventCapture()
    diagnostics = None

    def record_usage(usage: InvocationUsage) -> None:
        capture.usage = usage

    if telemetry is not None:
        event_context = metadata.event_context()

        def diagnostics_sink(diagnostic: InvocationDiagnostic) -> None:
            emit_runtime_log(
                telemetry,
                diagnostic.level,
                diagnostic.message,
                diagnostic.operation,
                context=event_context,
                attributes=diagnostic.attributes,
            )

        diagnostics = diagnostics_sink
    return (
        InvocationContext(
            node_id=metadata.node_id,
            task_id=metadata.task_id,
            provider=metadata.provider,
            role=metadata.role,
            audit_round_num=metadata.audit_round_num,
            round_num=metadata.round_num,
            findings_enabled=metadata.findings_enabled,
            diagnostics=diagnostics,
            usage_recorder=record_usage,
            console_message_sink=provider_console_message_sink(display),
        ),
        capture,
    )


def emit_provider_invocation_failure_event(
    telemetry: ExecutionTelemetry | None,
    agent_config: AgentConfig | None,
    invocation_metadata: InvocationMetadata | None,
    event_capture: InvocationEventCapture,
    timer: ElapsedTimer | None,
    exc: Exception,
    prompt: str,
    output_file: Path,
) -> None:
    if agent_config is None or invocation_metadata is None:
        return

    duration_ms = timer.elapsed_milliseconds if timer is not None else None
    try:
        emit_invocation_event(
            telemetry,
            "invocation_failed",
            invocation_metadata,
            duration_ms=duration_ms,
            error=safe_error_message(exc),
            usage=resolve_invocation_usage(
                capture=event_capture,
                agent_config=agent_config,
                prompt=prompt,
                output_file=output_file,
            ),
            **failure_event_fields(exc),
        )
    except Exception as telemetry_exc:
        exc.add_note(f"invocation failure telemetry failed: {telemetry_exc}")


def resolve_invocation_usage(
    capture: InvocationEventCapture,
    agent_config: AgentConfig,
    prompt: str,
    output_file: Path,
) -> InvocationUsage:
    if capture.usage is not None:
        return capture.usage
    return build_fallback_usage_from_output_file(prompt, output_file, agent_config)
