from __future__ import annotations

from orchestrator_cli.architecture.contracts import (
    AgentInvoker,
    LogPresentationDescriptor,
    validate_log_presentation_descriptor,
)
from orchestrator_cli.core.config import AgentConfig

from .activity.events import RuntimeEventContext, emit_runtime_log
from .activity.telemetry import ExecutionTelemetry

_INVALID_DESCRIPTOR_OPERATION = "log_presentation_descriptor_invalid"
_INVALID_DESCRIPTOR_MESSAGE = (
    "Log presentation metadata was unavailable; using plain log display."
)


def resolve_log_presentation_descriptor(
    invoker: AgentInvoker,
    config: AgentConfig,
    telemetry: ExecutionTelemetry | None,
    context: RuntimeEventContext,
) -> LogPresentationDescriptor | None:
    try:
        value = invoker.log_presentation_for(config)
    except Exception as exc:
        emit_invalid_descriptor_warning(
            telemetry,
            context,
            reason=exc.__class__.__name__,
        )
        return None

    if value is None:
        return None

    try:
        return validate_log_presentation_descriptor(value)
    except (TypeError, ValueError) as exc:
        emit_invalid_descriptor_warning(
            telemetry,
            context,
            reason=exc.__class__.__name__,
        )
        return None


def emit_invalid_descriptor_warning(
    telemetry: ExecutionTelemetry | None,
    context: RuntimeEventContext,
    reason: str,
) -> None:
    emit_runtime_log(
        telemetry,
        "warning",
        _INVALID_DESCRIPTOR_MESSAGE,
        _INVALID_DESCRIPTOR_OPERATION,
        context=context,
        attributes={"reason": reason},
    )
