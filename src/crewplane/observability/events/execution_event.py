from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol

from crewplane.observability.events.payloads import (
    EventPayload,
    validate_payload_type,
)
from crewplane.observability.events.types import EventType


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
    log_presentation_format: str | None = None
    log_presentation_profile: str | None = None

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
            "log_presentation_format": self.log_presentation_format,
            "log_presentation_profile": self.log_presentation_profile,
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


class EventSink(Protocol):
    """Callable event consumer used by runtime execution and observers."""

    def __call__(self, event: ExecutionEvent) -> None: ...


def emit_event(event_sink: EventSink | None, event: ExecutionEvent) -> None:
    """Emit an event when a sink is configured."""

    if event_sink is None:
        return
    event_sink(event)
