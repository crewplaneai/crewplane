from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .limits import DEFAULT_LIMITS, LogPresentationLimits
from .sanitize import clip_text, redact_json_value, sanitize_line

_CODEX_DIRECT_CONTENT_FIELDS = ("message", "content", "text", "delta")
_CODEX_ITEM_TEXT_FIELDS = ("text", "content")
_CODEX_ITEM_OUTPUT_FIELDS = (
    "output",
    "stdout",
    "stderr",
    "result",
    "aggregated_output",
)


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
    for key in _CODEX_DIRECT_CONTENT_FIELDS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return display_string_lines(value, limits)

    event_type = record.get("type") or record.get("event")
    if isinstance(event_type, str) and event_type.strip():
        item_event = _codex_item_event_line(record, event_type, limits)
        if item_event is not None:
            return [item_event]
        detail = _codex_detail(record, limits)
        label = event_type if not detail else f"{event_type}: {detail}"
        return [sanitize_line(label, limits)]
    item = record.get("item")
    if isinstance(item, Mapping):
        detail = _codex_item_detail(record, item, limits)
        if detail:
            return [sanitize_line(f"item: {detail}", limits)]
    return render_generic_record(record, limits)


def render_claude_object(
    record: Mapping[str, Any],
    limits: LogPresentationLimits,
) -> list[str]:
    lines: list[str] = []
    for key in ("result", "error"):
        value = record.get(key)
        if value not in (None, ""):
            if isinstance(value, str):
                lines.extend(display_string_lines(value, limits, label=key))
            else:
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
            return display_string_lines(value, limits, label=key)
    return [compact_json_line(record, limits)]


def display_string_lines(
    value: str,
    limits: LogPresentationLimits,
    label: str | None = None,
) -> list[str]:
    stripped = value.strip()
    if not stripped:
        return []
    physical_lines = stripped.replace("\r\n", "\n").split("\n")
    first_line = physical_lines[0]
    if label is not None:
        first_line = f"{label}: {first_line}"
    lines = [sanitize_line(first_line, limits)]
    lines.extend(sanitize_line(f"  {line}", limits) for line in physical_lines[1:])
    return lines


def _field(key: str, record: Mapping[str, Any]) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    return f"{key}={value}"


def _codex_detail(record: Mapping[str, Any], limits: LogPresentationLimits) -> str:
    for key in ("message", "content", "text"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return clip_text(value, limits.max_display_chars_per_record)
    item = record.get("item")
    if isinstance(item, Mapping):
        detail = _codex_item_detail(record, item, limits)
        if detail:
            return detail
    for key in ("status", "exit_code"):
        value = _display_field_value(record.get(key), limits)
        if value:
            return f"{key}: {value}"
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
    detail = _codex_item_detail(record, item, limits)
    if detail:
        return sanitize_line(f"{item_type} {phase}: {detail}", limits)
    if _codex_is_empty_web_search_event(item):
        return sanitize_line(f"{item_type} {phase}", limits)
    return None


def _codex_item_detail(
    record: Mapping[str, Any],
    item: Mapping[str, Any],
    limits: LogPresentationLimits,
) -> str | None:
    components: list[str] = []
    search_query = _codex_web_search_detail(item)
    if search_query:
        components.append(search_query)

    text = _first_display_value(item, _CODEX_ITEM_TEXT_FIELDS, limits)
    if text:
        components.append(text)

    command = _display_field_value(item.get("command"), limits)
    if command:
        components.append(f"command: {command}")

    for key in ("status", "exit_code"):
        raw_value = item.get(key)
        if raw_value is None:
            raw_value = record.get(key)
        value = _display_field_value(raw_value, limits)
        if value:
            components.append(f"{key}: {value}")

    for key in _CODEX_ITEM_OUTPUT_FIELDS:
        value = _display_field_value(item.get(key), limits)
        if value:
            components.append(f"{key}: {value}")

    if components:
        return " | ".join(components)
    return None


def _codex_web_search_detail(item: Mapping[str, Any]) -> str | None:
    for value in _codex_web_search_detail_candidates(item):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _codex_web_search_detail_candidates(item: Mapping[str, Any]) -> list[Any]:
    action = item.get("action")
    candidates: list[Any] = []
    if isinstance(action, Mapping):
        candidates.append(action.get("query"))
        queries = action.get("queries")
        if isinstance(queries, list):
            candidates.extend(queries)
    candidates.append(item.get("query"))
    return candidates


def _codex_is_empty_web_search_event(item: Mapping[str, Any]) -> bool:
    return _string_field(item, "type") == "web_search"


def _first_display_value(
    record: Mapping[str, Any],
    keys: tuple[str, ...],
    limits: LogPresentationLimits,
) -> str | None:
    for key in keys:
        value = _display_field_value(record.get(key), limits)
        if value:
            return value
    return None


def _display_field_value(
    value: Any,
    limits: LogPresentationLimits,
) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (bool, int, float)):
        return str(value)
    return compact_json_line(value, limits)


def _string_field(record: Mapping[str, Any], key: str) -> str | None:
    value = record.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
