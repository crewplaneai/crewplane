from __future__ import annotations

from collections.abc import Mapping

from crewplane.observability.timing import format_elapsed_seconds

from .models import (
    InvocationTimingSummary,
    InvocationUsageSummary,
    NodeOutcomeSummary,
    RunSummary,
)

STAGE_ROW_LIMIT = 12
DURATION_ROW_LIMIT = 8
ISSUE_LIMIT = 3
PROVIDER_ROW_LIMIT = 4
RESULT_PATH_LIMIT = 3


def format_count(value: int | None) -> str:
    return "n/a" if value is None else f"{value:,}"


def format_cost(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:.6f}"


def format_provider_tokens(provider_tokens: Mapping[str, int | None]) -> str:
    parts = [
        f"{bucket}={format_count(value) if value is not None else 'n/a'}"
        for bucket, value in provider_tokens.items()
    ]
    return ", ".join(parts)


def format_visible_estimate(invocation_usage: InvocationUsageSummary) -> str:
    if invocation_usage.visible_estimate_tokens is None:
        return "n/a"
    method = invocation_usage.visible_estimate_method or "unknown"
    suffix = " lower-bound" if invocation_usage.visible_estimate_is_lower_bound else ""
    return f"{format_count(invocation_usage.visible_estimate_tokens)} tokens via {method}{suffix}"


def format_round_label(
    audit_round_num: int | None, round_num: int | None
) -> str | None:
    if audit_round_num is not None and round_num is not None:
        return f"audit{audit_round_num}/round{round_num}"
    if audit_round_num is not None:
        return f"audit{audit_round_num}"
    if round_num is not None:
        return f"round{round_num}"
    return None


def invocation_label(
    node_id: str | None,
    task_id: str | None,
    audit_round_num: int | None,
    round_num: int | None,
) -> str:
    label_parts = []
    if node_id:
        label_parts.append(f"`{node_id}`")
    if task_id:
        label_parts.append(f"`{task_id}`")
    round_label = format_round_label(audit_round_num, round_num)
    if round_label is not None:
        label_parts.append(f"`{round_label}`")
    return " / ".join(label_parts) if label_parts else "`unknown invocation`"


def selected_stages(summary: RunSummary) -> tuple[NodeOutcomeSummary, ...]:
    nodes = summary.node_outcomes
    priority = [index for index, node in enumerate(nodes) if node.status != "succeeded"]
    remaining = [
        index for index, node in enumerate(nodes) if node.status == "succeeded"
    ]
    selected = sorted((priority + remaining)[:STAGE_ROW_LIMIT])
    return tuple(nodes[index] for index in selected)


def selected_timings(summary: RunSummary) -> tuple[InvocationTimingSummary, ...]:
    return tuple(
        sorted(summary.invocation_timings, key=timing_sort_key)[:DURATION_ROW_LIMIT]
    )


def timing_sort_key(
    timing: InvocationTimingSummary,
) -> tuple[bool, int, str, str, int, int, str, str]:
    return (
        timing.duration_ms is None,
        -(timing.duration_ms or 0),
        timing.node_id,
        timing.task_id,
        timing.audit_round_num if timing.audit_round_num is not None else -1,
        timing.round_num if timing.round_num is not None else -1,
        timing.provider,
        timing.model or "",
    )


def stage_status(node: NodeOutcomeSummary) -> str:
    if node.resume_kind == "reused":
        return f"{node.status} (reused)"
    if node.resume_kind == "checkpoint":
        return f"{node.status} (checkpoint resumed)"
    return node.status


def format_duration(seconds: float | None) -> str:
    return "n/a" if seconds is None else format_elapsed_seconds(seconds)


def format_call_duration(duration_ms: int | None) -> str:
    return "n/a" if duration_ms is None else f"{duration_ms / 1000:.3f}s"


def call_identity(timing: InvocationTimingSummary) -> str:
    parts = [timing.node_id, timing.task_id]
    round_label = format_round_label(timing.audit_round_num, timing.round_num)
    if round_label is not None:
        parts.append(round_label)
    return " / ".join(parts)


def stage_counts_label(summary: RunSummary) -> str:
    counts = summary.node_counts
    if counts is None:
        return "n/a (no state captured)"
    parts = [
        f"{value} {status}"
        for status, value in (
            ("succeeded", counts.succeeded),
            ("failed", counts.failed),
            ("cancelled", counts.cancelled),
            ("blocked", counts.blocked),
            ("running", counts.running),
            ("pending", counts.pending),
        )
        if value
    ]
    reused = sum(node.resume_kind == "reused" for node in summary.node_outcomes)
    if reused:
        parts.append(f"including {reused} reused")
    return ", ".join(parts) if parts else "0 stages"


def invocation_coverage_label(summary: RunSummary) -> str:
    captured = summary.spend.terminal_invocations if summary.spend else None
    if summary.invocation_count is None:
        return f"{format_count(captured)} calls with usage facts; total calls n/a"
    if summary.invocation_count == 0:
        return "0 calls (no new calls)"
    return (
        f"{format_count(captured)} of {summary.invocation_count} calls with usage facts"
    )
