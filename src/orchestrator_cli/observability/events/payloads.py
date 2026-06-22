from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from orchestrator_cli.architecture.contracts import OutputExtractionStatus
from orchestrator_cli.observability.events.types import (
    EventType,
    LogLevel,
    RuntimeLogValue,
)


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

    def as_event_fields(self) -> dict[str, object]:
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
