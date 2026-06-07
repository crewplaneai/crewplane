from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol

from orchestrator_cli.observability.events.payloads import (
    EventPayload,
    InvocationEventPayload,
    NodeEventPayload,
    RuntimeLogEventPayload,
    WorkflowEventPayload,
    validate_payload_type,
)
from orchestrator_cli.observability.events.types import EventType


@dataclass(frozen=True)
class ExecutionEventContext:
    """Shared context fields common to all execution event variants."""

    workflow_name: str
    run_id: str
    node_id: str | None = None
    provider: str | None = None
    role: str | None = None
    model: str | None = None
    task_id: str | None = None
    audit_round_num: int | None = None
    round_num: int | None = None
    output_file: str | None = None
    log_file: str | None = None

    def as_event_fields(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "provider": self.provider,
            "role": self.role,
            "model": self.model,
            "task_id": self.task_id,
            "audit_round_num": self.audit_round_num,
            "round_num": self.round_num,
            "output_file": self.output_file,
            "log_file": self.log_file,
        }


@dataclass(frozen=True)
class ExecutionEvent:
    """Single typed runtime event emitted by execution phases."""

    event_type: EventType
    workflow_name: str
    run_id: str
    context: ExecutionEventContext
    payload: EventPayload
    timestamp: float = field(default_factory=monotonic)
    timestamp_utc: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        if self.context.workflow_name != self.workflow_name:
            raise ValueError("Execution event workflow mismatch in context.")
        if self.context.run_id != self.run_id:
            raise ValueError("Execution event run_id mismatch in context.")
        validate_payload_type(self.event_type, self.payload)

    @property
    def node_id(self) -> str | None:
        return self.context.node_id

    @property
    def provider(self) -> str | None:
        return self.context.provider

    @property
    def role(self) -> str | None:
        return self.context.role

    @property
    def model(self) -> str | None:
        return self.context.model

    @property
    def task_id(self) -> str | None:
        return self.context.task_id

    @property
    def audit_round_num(self) -> int | None:
        return self.context.audit_round_num

    @property
    def round_num(self) -> int | None:
        return self.context.round_num

    @property
    def output_file(self) -> str | None:
        return self.context.output_file

    @property
    def log_file(self) -> str | None:
        return self.context.log_file

    @property
    def duration_ms(self) -> int | None:
        if isinstance(self.payload, (InvocationEventPayload, RuntimeLogEventPayload)):
            return self.payload.duration_ms
        return None

    @property
    def error(self) -> str | None:
        if isinstance(
            self.payload,
            (
                WorkflowEventPayload,
                InvocationEventPayload,
                NodeEventPayload,
                RuntimeLogEventPayload,
            ),
        ):
            return self.payload.error
        return None

    @property
    def attempt_count(self) -> int | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.attempt_count
        return None

    @property
    def cli_captured(self) -> bool | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.cli_captured
        return None

    @property
    def output_extraction_status(self) -> str | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.output_extraction_status
        return None

    @property
    def provider_usage_status(self) -> str | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.provider_usage_status
        return None

    @property
    def provider_tokens(self) -> object | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.provider_tokens
        return None

    @property
    def visible_estimate_tokens(self) -> int | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.visible_estimate_tokens
        return None

    @property
    def visible_estimate_method(self) -> str | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.visible_estimate_method
        return None

    @property
    def visible_estimate_is_lower_bound(self) -> bool | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.visible_estimate_is_lower_bound
        return None

    @property
    def configured_cost_usd(self) -> float | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.configured_cost_usd
        return None

    @property
    def invocation_cost_confidence(self) -> str | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.invocation_cost_confidence
        return None

    @property
    def usage_parse_error(self) -> str | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.usage_parse_error
        return None

    @property
    def failure_kind(self) -> str | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.failure_kind
        return None

    @property
    def failure_phase(self) -> str | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.failure_phase
        return None

    @property
    def failure_source(self) -> str | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.failure_source
        return None

    @property
    def failure_advice(self) -> str | None:
        if isinstance(self.payload, InvocationEventPayload):
            return self.payload.failure_advice
        return None

    @property
    def level(self) -> str | None:
        if isinstance(self.payload, RuntimeLogEventPayload):
            return self.payload.level
        return None

    @property
    def message(self) -> str | None:
        if isinstance(self.payload, RuntimeLogEventPayload):
            return self.payload.message
        return None

    @property
    def operation(self) -> str | None:
        if isinstance(self.payload, RuntimeLogEventPayload):
            return self.payload.operation
        return None

    @property
    def attributes(self) -> object | None:
        if isinstance(self.payload, RuntimeLogEventPayload):
            return self.payload.attributes
        return None


class EventSink(Protocol):
    """Callable event consumer used by runtime execution and observers."""

    def __call__(self, event: ExecutionEvent) -> None: ...


def emit_event(event_sink: EventSink | None, event: ExecutionEvent) -> None:
    """Emit an event when a sink is configured."""

    if event_sink is None:
        return
    event_sink(event)
