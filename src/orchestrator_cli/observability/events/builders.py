from __future__ import annotations

from collections.abc import Mapping

from orchestrator_cli.architecture.contracts import OutputExtractionStatus
from orchestrator_cli.observability.events.execution_event import (
    ExecutionEvent,
    ExecutionEventContext,
)
from orchestrator_cli.observability.events.payloads import (
    EventPayload,
    InvocationEventPayload,
    NodeEventPayload,
    RuntimeLogEventPayload,
    WorkflowEventPayload,
)
from orchestrator_cli.observability.events.types import (
    EventType,
    InvocationEventType,
    LogLevel,
    NodeEventType,
    RuntimeLogValue,
    WorkflowEventType,
)


def workflow_event(
    event_type: WorkflowEventType,
    workflow_name: str,
    run_id: str,
    *,
    error: str | None = None,
    timestamp: float | None = None,
    timestamp_utc: str | None = None,
) -> ExecutionEvent:
    return _build_event(
        event_type=event_type,
        workflow_name=workflow_name,
        run_id=run_id,
        context=ExecutionEventContext(workflow_name=workflow_name, run_id=run_id),
        payload=WorkflowEventPayload(error=error),
        timestamp=timestamp,
        timestamp_utc=timestamp_utc,
    )


def node_event(
    event_type: NodeEventType,
    workflow_name: str,
    run_id: str,
    node_id: str,
    *,
    error: str | None = None,
    timestamp: float | None = None,
    timestamp_utc: str | None = None,
) -> ExecutionEvent:
    return _build_event(
        event_type=event_type,
        workflow_name=workflow_name,
        run_id=run_id,
        context=ExecutionEventContext(
            workflow_name=workflow_name,
            run_id=run_id,
            node_id=node_id,
        ),
        payload=NodeEventPayload(error=error),
        timestamp=timestamp,
        timestamp_utc=timestamp_utc,
    )


def invocation_event(
    event_type: InvocationEventType,
    workflow_name: str,
    run_id: str,
    *,
    node_id: str,
    provider: str,
    role: str,
    task_id: str,
    model: str | None = None,
    audit_round_num: int | None = None,
    round_num: int | None = None,
    output_file: str | None = None,
    log_file: str | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    attempt_count: int | None = None,
    cli_captured: bool | None = None,
    output_extraction_status: OutputExtractionStatus | None = None,
    provider_usage_status: str | None = None,
    provider_tokens: Mapping[str, int | None] | None = None,
    visible_estimate_tokens: int | None = None,
    visible_estimate_method: str | None = None,
    visible_estimate_is_lower_bound: bool | None = None,
    configured_cost_usd: float | None = None,
    invocation_cost_confidence: str | None = None,
    usage_parse_error: str | None = None,
    failure_kind: str | None = None,
    failure_phase: str | None = None,
    failure_source: str | None = None,
    failure_advice: str | None = None,
    timestamp: float | None = None,
    timestamp_utc: str | None = None,
) -> ExecutionEvent:
    return _build_event(
        event_type=event_type,
        workflow_name=workflow_name,
        run_id=run_id,
        context=ExecutionEventContext(
            workflow_name=workflow_name,
            run_id=run_id,
            node_id=node_id,
            provider=provider,
            role=role,
            model=model,
            task_id=task_id,
            audit_round_num=audit_round_num,
            round_num=round_num,
            output_file=output_file,
            log_file=log_file,
        ),
        payload=InvocationEventPayload(
            duration_ms=duration_ms,
            error=error,
            attempt_count=attempt_count,
            cli_captured=cli_captured,
            output_extraction_status=output_extraction_status,
            provider_usage_status=provider_usage_status,
            provider_tokens=provider_tokens,
            visible_estimate_tokens=visible_estimate_tokens,
            visible_estimate_method=visible_estimate_method,
            visible_estimate_is_lower_bound=visible_estimate_is_lower_bound,
            configured_cost_usd=configured_cost_usd,
            invocation_cost_confidence=invocation_cost_confidence,
            usage_parse_error=usage_parse_error,
            failure_kind=failure_kind,
            failure_phase=failure_phase,
            failure_source=failure_source,
            failure_advice=failure_advice,
        ),
        timestamp=timestamp,
        timestamp_utc=timestamp_utc,
    )


def runtime_log_event(
    workflow_name: str,
    run_id: str,
    *,
    level: LogLevel,
    message: str,
    operation: str,
    node_id: str | None = None,
    provider: str | None = None,
    role: str | None = None,
    model: str | None = None,
    task_id: str | None = None,
    audit_round_num: int | None = None,
    round_num: int | None = None,
    output_file: str | None = None,
    log_file: str | None = None,
    attributes: Mapping[str, RuntimeLogValue] | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    timestamp: float | None = None,
    timestamp_utc: str | None = None,
) -> ExecutionEvent:
    return _build_event(
        event_type="runtime_log",
        workflow_name=workflow_name,
        run_id=run_id,
        context=ExecutionEventContext(
            workflow_name=workflow_name,
            run_id=run_id,
            node_id=node_id,
            provider=provider,
            role=role,
            model=model,
            task_id=task_id,
            audit_round_num=audit_round_num,
            round_num=round_num,
            output_file=output_file,
            log_file=log_file,
        ),
        payload=RuntimeLogEventPayload(
            level=level,
            message=message,
            operation=operation,
            attributes=attributes,
            duration_ms=duration_ms,
            error=error,
        ),
        timestamp=timestamp,
        timestamp_utc=timestamp_utc,
    )


def _build_event(
    *,
    event_type: EventType,
    workflow_name: str,
    run_id: str,
    context: ExecutionEventContext,
    payload: EventPayload,
    timestamp: float | None,
    timestamp_utc: str | None,
) -> ExecutionEvent:
    kwargs: dict[str, object] = {
        "event_type": event_type,
        "workflow_name": workflow_name,
        "run_id": run_id,
        "context": context,
        "payload": payload,
    }
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    if timestamp_utc is not None:
        kwargs["timestamp_utc"] = timestamp_utc
    return ExecutionEvent(**kwargs)
