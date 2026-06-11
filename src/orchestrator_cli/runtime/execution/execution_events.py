from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator_cli.observability.events import (
    EventType,
    LogLevel,
    RuntimeLogValue,
    emit_event,
    invocation_event,
    node_event,
    runtime_log_event,
    workflow_event,
)
from orchestrator_cli.runtime.agent.failures import InvocationFailureError
from orchestrator_cli.runtime.agent.usage import InvocationUsage

from .execution_activity import ExecutionTelemetry


@dataclass(frozen=True)
class RuntimeEventContext:
    node_id: str | None = None
    provider: str | None = None
    role: str | None = None
    model: str | None = None
    task_id: str | None = None
    audit_round_num: int | None = None
    round_num: int | None = None
    output_file: Path | None = None
    log_file: Path | None = None
    log_presentation_format: str | None = None
    log_presentation_profile: str | None = None

    def as_execution_event_fields(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "provider": self.provider,
            "role": self.role,
            "model": self.model,
            "task_id": self.task_id,
            "audit_round_num": self.audit_round_num,
            "round_num": self.round_num,
            "output_file": str(self.output_file) if self.output_file else None,
            "log_file": str(self.log_file) if self.log_file else None,
            "log_presentation_format": self.log_presentation_format,
            "log_presentation_profile": self.log_presentation_profile,
        }


@dataclass(frozen=True)
class InvocationMetadata:
    node_id: str
    provider: str
    role: str
    model: str | None
    task_id: str
    audit_round_num: int | None
    round_num: int
    output_file: Path
    log_file: Path | None
    findings_enabled: bool = False
    log_presentation_format: str | None = None
    log_presentation_profile: str | None = None

    def event_context(self) -> RuntimeEventContext:
        return RuntimeEventContext(
            node_id=self.node_id,
            provider=self.provider,
            role=self.role,
            model=self.model,
            task_id=self.task_id,
            audit_round_num=self.audit_round_num,
            round_num=self.round_num,
            output_file=self.output_file,
            log_file=self.log_file,
            log_presentation_format=self.log_presentation_format,
            log_presentation_profile=self.log_presentation_profile,
        )


@dataclass
class InvocationEventCapture:
    usage: InvocationUsage | None = None


def emit_workflow_event(
    telemetry: ExecutionTelemetry | None,
    event_type: EventType,
    node_id: str | None = None,
    provider: str | None = None,
    role: str | None = None,
    model: str | None = None,
    task_id: str | None = None,
    round_num: int | None = None,
    output_file: Path | None = None,
    log_file: Path | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    if telemetry is None:
        return
    if event_type in {"workflow_started", "workflow_finished", "workflow_failed"}:
        event = workflow_event(
            event_type,  # type: ignore[arg-type]
            workflow_name=telemetry.workflow_name,
            run_id=telemetry.run_id,
            error=error,
        )
    elif event_type in {"node_started", "node_finished", "node_failed", "node_blocked"}:
        if node_id is None:
            raise ValueError(f"Node event '{event_type}' requires node_id.")
        event = node_event(
            event_type,  # type: ignore[arg-type]
            workflow_name=telemetry.workflow_name,
            run_id=telemetry.run_id,
            node_id=node_id,
            error=error,
        )
    else:
        if node_id is None or provider is None or role is None or task_id is None:
            raise ValueError(
                f"Invocation event '{event_type}' requires invocation context."
            )
        event = invocation_event(
            event_type,  # type: ignore[arg-type]
            workflow_name=telemetry.workflow_name,
            run_id=telemetry.run_id,
            node_id=node_id,
            provider=provider,
            role=role,
            model=model,
            task_id=task_id,
            round_num=round_num,
            output_file=str(output_file) if output_file is not None else None,
            log_file=str(log_file) if log_file is not None else None,
            duration_ms=duration_ms,
            error=error,
        )
    emit_event(telemetry.event_sink, event)


def emit_runtime_log(
    telemetry: ExecutionTelemetry | None,
    level: LogLevel,
    message: str,
    operation: str,
    context: RuntimeEventContext | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    attributes: dict[str, RuntimeLogValue] | None = None,
) -> None:
    if telemetry is None:
        return
    event_fields = context.as_execution_event_fields() if context is not None else {}
    emit_event(
        telemetry.event_sink,
        runtime_log_event(
            workflow_name=telemetry.workflow_name,
            run_id=telemetry.run_id,
            level=level,
            message=message,
            operation=operation,
            attributes=dict(attributes) if attributes is not None else None,
            duration_ms=duration_ms,
            error=error,
            **event_fields,
        ),
    )


def emit_invocation_event(
    telemetry: ExecutionTelemetry | None,
    event_type: EventType,
    metadata: InvocationMetadata,
    duration_ms: int | None = None,
    error: str | None = None,
    usage: InvocationUsage | None = None,
    failure_kind: str | None = None,
    failure_phase: str | None = None,
    failure_source: str | None = None,
    failure_advice: str | None = None,
) -> None:
    if telemetry is None:
        return
    event_fields = metadata.event_context().as_execution_event_fields()
    usage_fields = usage.as_event_fields() if usage is not None else {}
    emit_event(
        telemetry.event_sink,
        invocation_event(
            event_type,  # type: ignore[arg-type]
            workflow_name=telemetry.workflow_name,
            run_id=telemetry.run_id,
            duration_ms=duration_ms,
            error=error,
            failure_kind=failure_kind,
            failure_phase=failure_phase,
            failure_source=failure_source,
            failure_advice=failure_advice,
            **event_fields,
            **usage_fields,
        ),
    )


def safe_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__


def failure_event_fields(exc: Exception) -> dict[str, str]:
    if not isinstance(exc, InvocationFailureError):
        return {}
    return {
        "failure_kind": exc.kind,
        "failure_phase": exc.phase,
        "failure_source": exc.source,
        "failure_advice": exc.advice,
    }
