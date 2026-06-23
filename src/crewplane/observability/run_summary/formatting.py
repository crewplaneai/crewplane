from __future__ import annotations

from collections.abc import Mapping

from crewplane.observability.timing import format_elapsed_seconds

from .models import InvocationUsageSummary


def format_count(value: int) -> str:
    return f"{value:,}"


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
    if audit_round_num is not None and round_num is not None:
        label_parts.append(f"`audit{audit_round_num}/round{round_num}`")
    elif audit_round_num is not None:
        label_parts.append(f"`audit{audit_round_num}`")
    elif round_num is not None:
        label_parts.append(f"`round{round_num}`")
    return " / ".join(label_parts) if label_parts else "`unknown invocation`"


def duration_label(
    started_at: float | None,
    finished_at: float | None,
    now: float,
) -> str:
    if started_at is None:
        return ""
    end = finished_at if finished_at is not None else now
    return f" in {format_elapsed_seconds(max(0.0, end - started_at))}"
