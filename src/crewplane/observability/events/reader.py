from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TypeGuard, get_args

from crewplane.architecture.contracts import OutputExtractionStatus
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events.execution_event import (
    ExecutionEvent,
    ExecutionEventContext,
)
from crewplane.observability.events.payloads import (
    EventPayload,
    InvocationEventPayload,
    NodeEventPayload,
    RuntimeLogEventPayload,
    WorkflowEventPayload,
    WorkspaceEventPayload,
)
from crewplane.observability.events.types import EventType, LogLevel, RuntimeLogValue

EVENT_TYPES: frozenset[str] = frozenset(get_args(EventType))
LOG_LEVELS: frozenset[str] = frozenset(get_args(LogLevel))
_OUTPUT_EXTRACTION_STATUSES: frozenset[str] = frozenset(
    get_args(OutputExtractionStatus)
)


def read_event_log(event_log_path: Path) -> list[ExecutionEvent]:
    """Read valid execution events from one durable NDJSON event log."""
    if not event_log_path.is_file() or event_log_path.is_symlink():
        return []
    try:
        lines = event_log_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    return [event for line in lines if (event := event_from_line(line)) is not None]


def event_from_line(line: str) -> ExecutionEvent | None:
    if not line.strip():
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    return event_from_record(record)


def event_from_record(record: Mapping[str, object]) -> ExecutionEvent | None:
    event_type = record.get("event_type")
    workflow_name = _string(record.get("workflow_name"))
    run_id = _string(record.get("run_id"))
    timestamp_utc = _string(record.get("timestamp"))
    if (
        not _is_event_type(event_type)
        or workflow_name is None
        or run_id is None
        or timestamp_utc is None
    ):
        return None
    payload = _payload_from_record(event_type, record)
    if payload is None:
        return None
    try:
        return ExecutionEvent(
            event_type=event_type,
            workflow_name=workflow_name,
            run_id=run_id,
            context=_context_from_record(workflow_name, run_id, record),
            payload=payload,
            timestamp=_timestamp_value(timestamp_utc),
            timestamp_utc=timestamp_utc,
        )
    except (TypeError, ValueError):
        return None


def _payload_from_record(
    event_type: EventType,
    record: Mapping[str, object],
) -> EventPayload | None:
    match event_type:
        case "workflow_started" | "workflow_finished" | "workflow_failed":
            return _workflow_payload_from_record(record)
        case "node_started" | "node_finished" | "node_failed" | "node_blocked":
            return _node_payload_from_record(record)
        case "invocation_started" | "invocation_finished" | "invocation_failed":
            return _invocation_payload_from_record(record)
        case "workspace_context_recorded":
            return _workspace_payload_from_record(record)
        case "runtime_log":
            return _runtime_log_payload_from_record(record)


def _workflow_payload_from_record(
    record: Mapping[str, object],
) -> WorkflowEventPayload:
    return WorkflowEventPayload(error=_string(record.get("error")))


def _node_payload_from_record(record: Mapping[str, object]) -> NodeEventPayload:
    return NodeEventPayload(error=_string(record.get("error")))


def _invocation_payload_from_record(
    record: Mapping[str, object],
) -> InvocationEventPayload | None:
    raw_report_count = record.get("provider_usage_report_count")
    report_count = _report_count(raw_report_count)
    if raw_report_count is not None and report_count is None:
        return None
    return InvocationEventPayload(
        duration_ms=_integer(record.get("duration_ms")),
        error=_string(record.get("error")),
        attempt_count=_integer(record.get("attempt_count")),
        cli_captured=_boolean(record.get("cli_captured")),
        output_extraction_status=_output_extraction_status(
            record.get("output_extraction_status")
        ),
        provider_usage_status=_string(record.get("provider_usage_status")),
        provider_usage_report_count=report_count,
        provider_tokens=_integer_mapping(record.get("provider_tokens")),
        visible_estimate_tokens=_integer(record.get("visible_estimate_tokens")),
        visible_estimate_method=_string(record.get("visible_estimate_method")),
        visible_estimate_is_lower_bound=_boolean(
            record.get("visible_estimate_is_lower_bound")
        ),
        configured_cost_usd=_float(record.get("configured_cost_usd")),
        invocation_cost_confidence=_string(record.get("invocation_cost_confidence")),
        usage_parse_error=_string(record.get("usage_parse_error")),
        failure_kind=_string(record.get("failure_kind")),
        failure_phase=_string(record.get("failure_phase")),
        failure_source=_string(record.get("failure_source")),
        failure_advice=_string(record.get("failure_advice")),
    )


def _workspace_payload_from_record(
    record: Mapping[str, object],
) -> WorkspaceEventPayload:
    return WorkspaceEventPayload(
        status=_string(record.get("status")),
        workspace_kind=_string(record.get("workspace_kind")),
        workspace_logical_worktree_name=_string(
            record.get("workspace_logical_worktree_name")
        ),
        workspace_materialization=_string(record.get("workspace_materialization")),
        workspace_source_kind=_string(record.get("workspace_source_kind")),
        workspace_source_node_id=_string(record.get("workspace_source_node_id")),
        workspace_source_commit=_string(record.get("workspace_source_commit")),
        workspace_source_tree=_string(record.get("workspace_source_tree")),
        worktree_contract_mode=_string(record.get("worktree_contract_mode")),
        worktree_contract_schema_version=_string(
            record.get("worktree_contract_schema_version")
        ),
        workspace_state_path=_string(record.get("workspace_state_path")),
        workspace_writable=_boolean(record.get("workspace_writable")),
        workspace_lineage_producer=_boolean(record.get("workspace_lineage_producer")),
        workspace_child_environment_required=_boolean(
            record.get("workspace_child_environment_required")
        ),
        workspace_child_environment_applied=_boolean(
            record.get("workspace_child_environment_applied")
        ),
    )


def _runtime_log_payload_from_record(
    record: Mapping[str, object],
) -> RuntimeLogEventPayload | None:
    level = _string(record.get("level"))
    message = _string(record.get("message"))
    operation = _string(record.get("operation"))
    if not _is_log_level(level) or message is None or operation is None:
        return None
    return RuntimeLogEventPayload(
        level=level,
        message=message,
        operation=operation,
        attributes=_runtime_log_attributes(record.get("attributes")),
        duration_ms=_integer(record.get("duration_ms")),
        error=_string(record.get("error")),
    )


def _context_from_record(
    workflow_name: str,
    run_id: str,
    record: Mapping[str, object],
) -> ExecutionEventContext:
    return ExecutionEventContext(
        workflow_name=workflow_name,
        run_id=run_id,
        node_id=_string(record.get("node_id")),
        provider=_string(record.get("provider")),
        role=_provider_role(record.get("role")),
        model=_string(record.get("model")),
        task_id=_string(record.get("task_id")),
        audit_round_num=_integer(record.get("audit_round_num")),
        round_num=_integer(record.get("round_num")),
        output_file=_string(record.get("output_file")),
        log_file=_string(record.get("log_file")),
        log_presentation_format=_string(record.get("log_presentation_format")),
        log_presentation_profile=_string(record.get("log_presentation_profile")),
    )


def _timestamp_value(timestamp_utc: str) -> float:
    try:
        return datetime.fromisoformat(timestamp_utc).timestamp()
    except ValueError:
        return 0.0


def _is_event_type(value: object) -> TypeGuard[EventType]:
    return isinstance(value, str) and value in EVENT_TYPES


def _is_log_level(value: str | None) -> TypeGuard[LogLevel]:
    return isinstance(value, str) and value in LOG_LEVELS


def _output_extraction_status(value: object) -> OutputExtractionStatus | None:
    if _is_output_extraction_status(value):
        return value
    return None


def _is_output_extraction_status(
    value: object,
) -> TypeGuard[OutputExtractionStatus]:
    return isinstance(value, str) and value in _OUTPUT_EXTRACTION_STATUSES


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _provider_role(value: object) -> ProviderRole | None:
    role = _string(value)
    return ProviderRole(role) if role is not None else None


def _integer(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _report_count(value: object) -> int | None:
    if value is None:
        return None
    integer = _integer(value)
    if integer is None or integer < 0:
        return None
    return integer


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _integer_mapping(value: object) -> Mapping[str, int | None] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, int | None] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        if item is not None and not (
            isinstance(item, int) and not isinstance(item, bool)
        ):
            return None
        result[key] = item
    return result


def _runtime_log_attributes(
    value: object,
) -> Mapping[str, RuntimeLogValue] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, RuntimeLogValue] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _is_runtime_log_value(item):
            return None
        result[key] = item
    return result


def _is_runtime_log_value(value: object) -> TypeGuard[RuntimeLogValue]:
    return value is None or isinstance(value, str | int | float | bool)
