from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .limits import DEFAULT_LIMITS, LogPresentationLimits
from .sanitize import clip_text, redact_json_value, sanitize_line


def exceeds_json_depth(value: Any, max_depth: int) -> bool:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            return True
        next_depth = depth + 1
        if isinstance(current, Mapping):
            stack.extend((item, next_depth) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, next_depth) for item in current)
    return False


def compact_json_line(
    value: Any,
    limits: LogPresentationLimits = DEFAULT_LIMITS,
) -> str:
    redacted = redact_json_value(value)
    try:
        rendered = json.dumps(redacted, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = repr(redacted)
    return clip_text(rendered, limits.max_display_chars_per_record)


def render_json_record(
    record: Any,
    profile: str,
    limits: LogPresentationLimits = DEFAULT_LIMITS,
) -> list[str]:
    if not isinstance(record, Mapping):
        return [compact_json_line(record, limits)]

    redacted = redact_json_value(record)
    if profile == "mock":
        return render_mock_record(redacted, limits)
    if profile == "codex":
        return render_codex_record(redacted, limits)
    return render_generic_record(redacted, limits)


def render_json_object(
    value: Any,
    profile: str,
    limits: LogPresentationLimits = DEFAULT_LIMITS,
) -> list[str]:
    if not isinstance(value, Mapping):
        return [compact_json_line(value, limits)]
    redacted = redact_json_value(value)
    if profile == "claude":
        return render_claude_object(redacted, limits)
    return render_generic_record(redacted, limits)


def render_mock_record(
    record: Mapping[str, Any],
    limits: LogPresentationLimits,
) -> list[str]:
    fields = [
        _field("source", record),
        _field("output_mode", record),
        _field("node_id", record),
        _field("task_id", record),
    ]
    details = " ".join(field for field in fields if field)
    return [sanitize_line(f"mock: {details or compact_json_line(record)}", limits)]


def render_codex_record(
    record: Mapping[str, Any],
    limits: LogPresentationLimits,
) -> list[str]:
    for key in ("message", "content", "text", "delta"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return [sanitize_line(value, limits)]

    event_type = record.get("type") or record.get("event")
    if isinstance(event_type, str) and event_type.strip():
        item_event = _codex_item_event_line(record, event_type, limits)
        if item_event is not None:
            return [item_event]
        detail = _codex_detail(record, limits)
        label = event_type if not detail else f"{event_type}: {detail}"
        return [sanitize_line(label, limits)]
    return render_generic_record(record, limits)


def render_claude_object(
    record: Mapping[str, Any],
    limits: LogPresentationLimits,
) -> list[str]:
    lines: list[str] = []
    for key in ("result", "error"):
        value = record.get(key)
        if value not in (None, ""):
            lines.append(sanitize_line(f"{key}: {value}", limits))
    usage = record.get("usage")
    if usage is not None:
        lines.append(
            sanitize_line(f"usage: {compact_json_line(usage, limits)}", limits)
        )
    for key in ("total_cost_usd", "duration_ms", "num_turns"):
        value = record.get(key)
        if value is not None:
            lines.append(sanitize_line(f"{key}: {value}", limits))
    return lines or render_generic_record(record, limits)


def render_generic_record(
    record: Mapping[str, Any],
    limits: LogPresentationLimits,
) -> list[str]:
    for key in ("message", "content", "text", "result", "error"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return [sanitize_line(f"{key}: {value}", limits)]
    return [compact_json_line(record, limits)]


def _field(key: str, record: Mapping[str, Any]) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    return f"{key}={value}"


def _codex_detail(record: Mapping[str, Any], limits: LogPresentationLimits) -> str:
    for key in ("message", "content", "text", "status"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return clip_text(value, limits.max_display_chars_per_record)
    return compact_json_line(record, limits)


def _codex_item_event_line(
    record: Mapping[str, Any],
    event_type: str,
    limits: LogPresentationLimits,
) -> str | None:
    if not event_type.startswith("item."):
        return None
    item = record.get("item")
    if not isinstance(item, Mapping):
        return None

    phase = event_type.removeprefix("item.")
    item_type = _string_field(item, "type") or "item"
    detail = _codex_item_detail(item)
    if detail:
        return sanitize_line(f"{item_type} {phase}: {detail}", limits)
    return sanitize_line(f"{item_type} {phase}", limits)


def _codex_item_detail(item: Mapping[str, Any]) -> str | None:
    for value in _codex_item_detail_candidates(item):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _codex_item_detail_candidates(item: Mapping[str, Any]) -> list[Any]:
    action = item.get("action")
    candidates: list[Any] = []
    if isinstance(action, Mapping):
        candidates.append(action.get("query"))
        queries = action.get("queries")
        if isinstance(queries, list):
            candidates.extend(queries)
    candidates.append(item.get("query"))
    return candidates


def _string_field(record: Mapping[str, Any], key: str) -> str | None:
    value = record.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
