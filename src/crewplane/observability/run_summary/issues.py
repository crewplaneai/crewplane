from __future__ import annotations

from crewplane.observability.events import (
    ExecutionEvent,
    InvocationEventPayload,
    NodeEventPayload,
    RuntimeLogEventPayload,
    WorkflowEventPayload,
)

from .models import IssueSummary


def issue_summaries(events: list[ExecutionEvent]) -> tuple[IssueSummary, ...]:
    issues = sorted(collect_issues(events), key=issue_sort_key)
    if not issues:
        return ()
    return tuple(
        IssueSummary(
            level="error" if severity == 0 else "warning",
            timestamp_utc=timestamp,
            message=message,
        )
        for severity, timestamp, message in issues
    )


def collect_issues(events: list[ExecutionEvent]) -> list[tuple[int, str, str]]:
    blocked_runtime_nodes = {
        event.context.node_id
        for event in events
        if event.event_type == "runtime_log"
        and runtime_payload(event).operation == "blocked_dependencies"
        and event.context.node_id is not None
    }
    issues: list[tuple[int, str, str]] = []
    for event in events:
        if event.event_type == "runtime_log":
            payload = runtime_payload(event)
            if payload.level not in {"warning", "error"} or not payload.message:
                continue
            issues.append(
                (
                    severity_rank(payload.level),
                    event.timestamp_utc,
                    format_runtime_issue(event, payload),
                )
            )
            continue
        if event.event_type == "invocation_failed":
            issues.append(
                (
                    severity_rank("error"),
                    event.timestamp_utc,
                    format_failure_issue("Invocation failed", event),
                )
            )
            continue
        if event.event_type == "node_failed":
            issues.append(
                (
                    severity_rank("error"),
                    event.timestamp_utc,
                    format_failure_issue("Node failed", event),
                )
            )
            continue
        if event.event_type == "workflow_failed":
            issues.append(
                (
                    severity_rank("error"),
                    event.timestamp_utc,
                    format_failure_issue("Workflow failed", event),
                )
            )
            continue
        if event.event_type == "node_blocked":
            if event.context.node_id in blocked_runtime_nodes:
                continue
            issues.append(
                (
                    severity_rank("warning"),
                    event.timestamp_utc,
                    format_failure_issue("Node blocked", event),
                )
            )
    return issues


def issue_sort_key(issue: tuple[int, str, str]) -> tuple[int, str]:
    severity, timestamp, _ = issue
    return severity, timestamp


def runtime_payload(event: ExecutionEvent) -> RuntimeLogEventPayload:
    if not isinstance(event.payload, RuntimeLogEventPayload):
        raise TypeError(
            f"Expected runtime-log payload for event_type '{event.event_type}'."
        )
    return event.payload


def format_runtime_issue(event: ExecutionEvent, payload: RuntimeLogEventPayload) -> str:
    if payload.operation == "stderr_fallback":
        label = (
            "Invocation succeeded with empty stdout; used stderr as output. "
            "Provider log contains the original stderr lines."
        )
    else:
        label = payload.message or "runtime warning"
    details = event_detail_segments(event)
    if not details:
        return f"[{payload.level}] {label}"
    return f"[{payload.level}] {label} ({'; '.join(details)})"


def format_failure_issue(prefix: str, event: ExecutionEvent) -> str:
    label = error_label(event)
    details = event_detail_segments(event)
    if not details:
        return f"[error] {prefix}: {label}"
    return f"[error] {prefix}: {label} ({'; '.join(details)})"


def error_label(event: ExecutionEvent) -> str:
    payload = event.payload
    if isinstance(payload, RuntimeLogEventPayload):
        return payload.error or payload.message or "unspecified error"
    if isinstance(payload, (WorkflowEventPayload, NodeEventPayload)):
        return payload.error or "unspecified error"
    if isinstance(payload, InvocationEventPayload):
        return payload.error or "unspecified error"
    return "unspecified error"


def event_detail_segments(event: ExecutionEvent) -> list[str]:
    details: list[str] = []
    context = event.context
    payload = event.payload
    if isinstance(payload, InvocationEventPayload):
        if payload.failure_kind:
            details.append(f"failure: {payload.failure_kind}")
        if payload.failure_advice:
            details.append(f"advice: {payload.failure_advice}")
    if context.node_id:
        details.append(f"node: {context.node_id}")
    if context.task_id:
        details.append(f"task: {context.task_id}")
    if context.audit_round_num is not None and context.round_num is not None:
        details.append(
            f"round: audit{context.audit_round_num}/round{context.round_num}"
        )
    elif context.audit_round_num is not None:
        details.append(f"round: audit{context.audit_round_num}")
    elif context.round_num is not None:
        details.append(f"round: round{context.round_num}")
    if context.output_file:
        details.append(f"output: {context.output_file}")
    if context.log_file:
        details.append(f"log: {context.log_file}")
    return details


def severity_rank(level: str) -> int:
    if level == "error":
        return 0
    return 1
