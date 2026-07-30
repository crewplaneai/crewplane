from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from types import MappingProxyType
from typing import Literal, Protocol, cast

from crewplane.architecture.contracts.invocation import (
    OutputExtractionStatus,
    RuntimeLogValue,
)
from crewplane.core.workflow.keywords import ProviderRole

from .json import JsonObject

EventType = Literal[
    "workflow_started",
    "workflow_finished",
    "workflow_failed",
    "node_started",
    "node_finished",
    "node_failed",
    "node_blocked",
    "invocation_started",
    "invocation_finished",
    "invocation_failed",
    "workspace_context_recorded",
    "runtime_log",
]
WorkflowEventType = Literal[
    "workflow_started",
    "workflow_finished",
    "workflow_failed",
]
NodeEventType = Literal[
    "node_started",
    "node_finished",
    "node_failed",
    "node_blocked",
]
InvocationEventType = Literal[
    "invocation_started",
    "invocation_finished",
    "invocation_failed",
]
WorkspaceEventType = Literal["workspace_context_recorded"]
WorkflowStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]
NodeStatus = Literal["pending", "running", "succeeded", "failed", "blocked"]
InvocationStatus = Literal["pending", "running", "succeeded", "failed"]
LogLevel = Literal["debug", "info", "warning", "error"]


@dataclass(frozen=True)
class EventPayload:
    """Base payload for a typed execution event variant."""

    def as_event_fields(self) -> JsonObject:
        return {}


@dataclass(frozen=True)
class WorkflowEventPayload(EventPayload):
    """Payload for workflow lifecycle events."""

    error: str | None = None

    def as_event_fields(self) -> JsonObject:
        return {"error": self.error}


@dataclass(frozen=True)
class NodeEventPayload(EventPayload):
    """Payload for node lifecycle events."""

    error: str | None = None

    def as_event_fields(self) -> JsonObject:
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

    def as_event_fields(self) -> JsonObject:
        return {
            "attempt_count": self.attempt_count,
            "cli_captured": self.cli_captured,
            "output_extraction_status": self.output_extraction_status,
            "provider_usage_status": self.provider_usage_status,
            "provider_tokens": (
                cast(JsonObject, dict(self.provider_tokens))
                if self.provider_tokens is not None
                else None
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
class WorkspaceEventPayload(EventPayload):
    """Payload for workspace facts linked to an invocation."""

    status: str | None = None
    workspace_kind: str | None = None
    workspace_logical_worktree_name: str | None = None
    workspace_materialization: str | None = None
    workspace_source_kind: str | None = None
    workspace_source_node_id: str | None = None
    workspace_source_commit: str | None = None
    workspace_source_tree: str | None = None
    worktree_contract_mode: str | None = None
    worktree_contract_schema_version: str | None = None
    workspace_state_path: str | None = None
    workspace_writable: bool | None = None
    workspace_lineage_producer: bool | None = None
    workspace_child_environment_required: bool | None = None
    workspace_child_environment_applied: bool | None = None

    def as_event_fields(self) -> JsonObject:
        return {
            "status": self.status,
            "workspace_kind": self.workspace_kind,
            "workspace_logical_worktree_name": self.workspace_logical_worktree_name,
            "workspace_materialization": self.workspace_materialization,
            "workspace_source_kind": self.workspace_source_kind,
            "workspace_source_node_id": self.workspace_source_node_id,
            "workspace_source_commit": self.workspace_source_commit,
            "workspace_source_tree": self.workspace_source_tree,
            "worktree_contract_mode": self.worktree_contract_mode,
            "worktree_contract_schema_version": self.worktree_contract_schema_version,
            "workspace_state_path": self.workspace_state_path,
            "workspace_writable": self.workspace_writable,
            "workspace_lineage_producer": self.workspace_lineage_producer,
            "workspace_child_environment_required": (
                self.workspace_child_environment_required
            ),
            "workspace_child_environment_applied": (
                self.workspace_child_environment_applied
            ),
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

    def as_event_fields(self) -> JsonObject:
        return {
            "level": self.level,
            "message": self.message,
            "operation": self.operation,
            "attributes": (
                cast(JsonObject, dict(self.attributes))
                if self.attributes is not None
                else None
            ),
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass(frozen=True)
class ExecutionEventContext:
    """Shared context fields common to all execution event variants."""

    workflow_name: str
    run_id: str
    node_id: str | None = None
    provider: str | None = None
    role: ProviderRole | None = None
    model: str | None = None
    requested_reasoning: str | None = None
    task_id: str | None = None
    audit_round_num: int | None = None
    round_num: int | None = None
    output_file: str | None = None
    log_file: str | None = None
    log_presentation_format: str | None = None
    log_presentation_profile: str | None = None

    def __post_init__(self) -> None:
        if self.role is not None:
            object.__setattr__(self, "role", ProviderRole(self.role))

    def as_event_fields(self) -> JsonObject:
        return {
            "node_id": self.node_id,
            "provider": self.provider,
            "role": self.role.value if self.role is not None else None,
            "model": self.model,
            "requested_reasoning": self.requested_reasoning,
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
        or (
            event_type == "workspace_context_recorded"
            and isinstance(payload, WorkspaceEventPayload)
        )
    )
    if not valid:
        raise ValueError(
            f"Execution event payload {payload.__class__.__name__} is not valid "
            f"for event_type '{event_type}'."
        )
