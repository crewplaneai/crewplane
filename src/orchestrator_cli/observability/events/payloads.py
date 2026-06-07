from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from orchestrator_cli.observability.events.types import (
    EventType,
    LogLevel,
    RuntimeLogValue,
)
from orchestrator_cli.runtime.agent.usage import OutputExtractionStatus


@dataclass(frozen=True)
class EventPayload:
    """Base payload for a typed execution event variant."""

    def as_event_fields(self) -> dict[str, object]:
        return {}


@dataclass(frozen=True)
class WorkflowEventPayload(EventPayload):
    """Payload for workflow lifecycle events."""

    error: str | None = None

    def as_event_fields(self) -> dict[str, object]:
        return {"error": self.error}


@dataclass(frozen=True)
class NodeEventPayload(EventPayload):
    """Payload for node lifecycle events."""

    error: str | None = None

    def as_event_fields(self) -> dict[str, object]:
        return {"error": self.error}


@dataclass(frozen=True)
class InvocationEventPayload(EventPayload):
    """Payload for invocation lifecycle events."""

    duration_ms: int | None = None
    error: str | None = None
    attempt_count: int | None = None
    cli_captured: bool | None = None
    output_extraction_status: OutputExtractionStatus | None = None
    provider_usage_status: str | None = None
    provider_tokens: Mapping[str, int | None] | None = None
    visible_estimate_tokens: int | None = None
    visible_estimate_method: str | None = None
    visible_estimate_is_lower_bound: bool | None = None
    configured_cost_usd: float | None = None
    invocation_cost_confidence: str | None = None
    usage_parse_error: str | None = None
    failure_kind: str | None = None
    failure_phase: str | None = None
    failure_source: str | None = None
    failure_advice: str | None = None

    def __post_init__(self) -> None:
        if self.provider_tokens is not None:
            object.__setattr__(
                self,
                "provider_tokens",
                MappingProxyType(dict(self.provider_tokens)),
            )

    def as_event_fields(self) -> dict[str, object]:
        return {
            "attempt_count": self.attempt_count,
            "cli_captured": self.cli_captured,
            "output_extraction_status": self.output_extraction_status,
            "provider_usage_status": self.provider_usage_status,
            "provider_tokens": (
                dict(self.provider_tokens) if self.provider_tokens is not None else None
            ),
            "visible_estimate_tokens": self.visible_estimate_tokens,
            "visible_estimate_method": self.visible_estimate_method,
            "visible_estimate_is_lower_bound": self.visible_estimate_is_lower_bound,
            "configured_cost_usd": self.configured_cost_usd,
            "invocation_cost_confidence": self.invocation_cost_confidence,
            "usage_parse_error": self.usage_parse_error,
            "failure_kind": self.failure_kind,
            "failure_phase": self.failure_phase,
            "failure_source": self.failure_source,
            "failure_advice": self.failure_advice,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass(frozen=True)
class RuntimeLogEventPayload(EventPayload):
    """Payload for runtime log events."""

    level: LogLevel
    message: str
    operation: str
    attributes: Mapping[str, RuntimeLogValue] | None = None
    duration_ms: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.attributes is not None:
            object.__setattr__(
                self,
                "attributes",
                MappingProxyType(dict(self.attributes)),
            )

    def as_event_fields(self) -> dict[str, object]:
        return {
            "level": self.level,
            "message": self.message,
            "operation": self.operation,
            "attributes": (
                dict(self.attributes) if self.attributes is not None else None
            ),
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


def validate_payload_type(event_type: EventType, payload: EventPayload) -> None:
    valid = (
        (
            event_type in {"workflow_started", "workflow_finished", "workflow_failed"}
            and isinstance(payload, WorkflowEventPayload)
        )
        or (event_type == "runtime_log" and isinstance(payload, RuntimeLogEventPayload))
        or (
            event_type
            in {"node_started", "node_finished", "node_failed", "node_blocked"}
            and isinstance(payload, NodeEventPayload)
        )
        or (
            event_type
            in {"invocation_started", "invocation_finished", "invocation_failed"}
            and isinstance(payload, InvocationEventPayload)
        )
    )
    if not valid:
        raise ValueError(
            f"Execution event payload {payload.__class__.__name__} is not valid "
            f"for event_type '{event_type}'."
        )
