from __future__ import annotations

from orchestrator_cli.observability.events import ExecutionEvent

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
        event.node_id
        for event in events
        if event.event_type == "runtime_log"
        and event.operation == "blocked_dependencies"
        and event.node_id is not None
    }
    issues: list[tuple[int, str, str]] = []
    for event in events:
        if event.event_type == "runtime_log":
            if event.level not in {"warning", "error"} or not event.message:
                continue
            issues.append(
                (
                    severity_rank(event.level),
                    event.timestamp_utc,
                    format_runtime_issue(event),
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
            if event.node_id in blocked_runtime_nodes:
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


def format_runtime_issue(event: ExecutionEvent) -> str:
    if event.operation == "stderr_fallback":
        label = (
            "Invocation succeeded with empty stdout; used stderr as output. "
            "Provider log contains the original stderr lines."
        )
    else:
        label = event.message or "runtime warning"
    details = event_detail_segments(event)
    if not details:
        return f"[{event.level}] {label}"
    return f"[{event.level}] {label} ({'; '.join(details)})"


def format_failure_issue(prefix: str, event: ExecutionEvent) -> str:
    label = event.error or event.message or "unspecified error"
    details = event_detail_segments(event)
    if not details:
        return f"[error] {prefix}: {label}"
    return f"[error] {prefix}: {label} ({'; '.join(details)})"


def event_detail_segments(event: ExecutionEvent) -> list[str]:
    details: list[str] = []
    if event.failure_kind:
        details.append(f"failure: {event.failure_kind}")
    if event.failure_advice:
        details.append(f"advice: {event.failure_advice}")
    if event.node_id:
        details.append(f"node: {event.node_id}")
    if event.task_id:
        details.append(f"task: {event.task_id}")
    if event.audit_round_num is not None and event.round_num is not None:
        details.append(f"round: audit{event.audit_round_num}/round{event.round_num}")
    elif event.audit_round_num is not None:
        details.append(f"round: audit{event.audit_round_num}")
    elif event.round_num is not None:
        details.append(f"round: round{event.round_num}")
    if event.output_file:
        details.append(f"output: {event.output_file}")
    if event.log_file:
        details.append(f"log: {event.log_file}")
    return details


def severity_rank(level: str) -> int:
    if level == "error":
        return 0
    return 1
