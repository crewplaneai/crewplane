from __future__ import annotations

import json
from collections.abc import Mapping

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


def exceeds_json_depth(value: object, max_depth: int) -> bool:
    stack: list[tuple[object, int]] = [(value, 1)]
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
    value: object,
    limits: LogPresentationLimits = DEFAULT_LIMITS,
) -> str:
    redacted = redact_json_value(value)
    try:
        rendered = json.dumps(
            redacted,
            allow_nan=False,
            sort_keys=True,
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        rendered = repr(redacted)
    return clip_text(rendered, limits.max_display_chars_per_record)


def render_json_record(
    record: object,
    profile: str,
    limits: LogPresentationLimits = DEFAULT_LIMITS,
) -> list[str]:
    if not isinstance(record, Mapping):
        return [compact_json_line(record, limits)]

    redacted = redact_json_value(record)
    if not isinstance(redacted, Mapping):
        return [compact_json_line(redacted, limits)]
    if profile == "mock":
        return render_mock_record(redacted, limits)
    if profile == "codex":
        return render_codex_record(redacted, limits)
    if profile == "kilo":
        return render_kilo_record(redacted, limits)
    return render_generic_record(redacted, limits)


def render_json_object(
    value: object,
    profile: str,
    limits: LogPresentationLimits = DEFAULT_LIMITS,
) -> list[str]:
    if not isinstance(value, Mapping):
        return [compact_json_line(value, limits)]
    redacted = redact_json_value(value)
    if not isinstance(redacted, Mapping):
        return [compact_json_line(redacted, limits)]
    if profile == "claude":
        return render_claude_object(redacted, limits)
    if profile == "gemini":
        return render_gemini_object(redacted, limits)
    return render_generic_record(redacted, limits)


def render_mock_record(
    record: Mapping[object, object],
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
    record: Mapping[object, object],
    limits: LogPresentationLimits,
) -> list[str]:
    for key in _CODEX_DIRECT_CONTENT_FIELDS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return display_string_lines(value, limits)

    event_type = record.get("type") or record.get("event")
    if isinstance(event_type, str) and event_type.strip():
        item_event = _codex_item_event_lines(record, event_type, limits)
        if item_event is not None:
            return item_event
        detail = _codex_detail(record, limits)
        label = event_type if not detail else f"{event_type}: {detail}"
        return [sanitize_line(label, limits)]
    item = record.get("item")
    if isinstance(item, Mapping):
        item_detail = _codex_item_detail(record, item, limits)
        if item_detail:
            return [sanitize_line(f"item: {item_detail}", limits)]
    return render_generic_record(record, limits)


def render_kilo_record(
    record: Mapping[object, object],
    limits: LogPresentationLimits,
) -> list[str]:
    event_type = _string_field(record, "type")
    if event_type == "step_finish":
        return []

    part = record.get("part")
    if not isinstance(part, Mapping):
        return render_generic_record(record, limits)
    text = _string_field(part, "text")
    if event_type == "text" and text:
        return display_string_lines(text, limits)
    return render_generic_record(record, limits)


def render_gemini_object(
    record: Mapping[object, object],
    limits: LogPresentationLimits,
) -> list[str]:
    response = _string_field(record, "response")
    if response:
        return display_string_lines(response, limits)
    return render_generic_record(record, limits)


def render_claude_object(
    record: Mapping[object, object],
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
    record: Mapping[object, object],
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


def _field(key: str, record: Mapping[object, object]) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    return f"{key}={value}"


def _codex_detail(
    record: Mapping[object, object],
    limits: LogPresentationLimits,
) -> str:
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


def _codex_item_event_lines(
    record: Mapping[object, object],
    event_type: str,
    limits: LogPresentationLimits,
) -> list[str] | None:
    if not event_type.startswith("item."):
        return None
    item = record.get("item")
    if not isinstance(item, Mapping):
        return None

    phase = event_type.removeprefix("item.")
    item_type = _string_field(item, "type") or "item"
    command_execution = _codex_command_execution_lines(
        record, item, item_type, phase, limits
    )
    if command_execution is not None:
        return command_execution
    detail = _codex_item_detail(record, item, limits)
    if detail:
        return [sanitize_line(f"{item_type} {phase}: {detail}", limits)]
    if _codex_is_empty_web_search_event(item):
        return [sanitize_line(f"{item_type} {phase}", limits)]
    return None


def _codex_command_execution_lines(
    record: Mapping[object, object],
    item: Mapping[object, object],
    item_type: str,
    phase: str,
    limits: LogPresentationLimits,
) -> list[str] | None:
    if item_type != "command_execution":
        return None

    command = _string_field(item, "command")
    output = item.get("aggregated_output")
    if command and "\n" in command:
        metadata = _codex_command_status_metadata(record, item, limits)
        body = display_string_lines(command, limits, label="command")
        text = _first_display_value(item, _CODEX_ITEM_TEXT_FIELDS, limits)
        if text:
            body.extend(display_string_lines(text, limits))
        for key in _CODEX_ITEM_OUTPUT_FIELDS:
            value = _display_field_value(item.get(key), limits)
            if value:
                body.extend(display_string_lines(value, limits, label=key))
    elif isinstance(output, str) and output.strip() and "\n" in output:
        metadata = _codex_command_execution_metadata(record, item, limits)
        body = display_string_lines(output, limits, label="aggregated_output")
    else:
        return None

    first_line = f"{item_type} {phase}"
    if metadata:
        first_line = f"{first_line}: {metadata}"
    return [sanitize_line(first_line, limits), *body]


def _codex_command_execution_metadata(
    record: Mapping[object, object],
    item: Mapping[object, object],
    limits: LogPresentationLimits,
) -> str:
    components: list[str] = []
    command = _display_field_value(item.get("command"), limits)
    if command:
        components.append(f"command: {command}")
    status = _codex_command_status_metadata(record, item, limits)
    if status:
        components.append(status)
    return " | ".join(components)


def _codex_command_status_metadata(
    record: Mapping[object, object],
    item: Mapping[object, object],
    limits: LogPresentationLimits,
) -> str:
    components: list[str] = []
    for key in ("status", "exit_code"):
        raw_value = item.get(key)
        if raw_value is None:
            raw_value = record.get(key)
        value = _display_field_value(raw_value, limits)
        if value:
            components.append(f"{key}: {value}")
    return " | ".join(components)


def _codex_item_detail(
    record: Mapping[object, object],
    item: Mapping[object, object],
    limits: LogPresentationLimits,
) -> str | None:
    components: list[str] = []
    search_query = _codex_web_search_detail(item)
    if search_query:
        components.append(search_query)

    text = _first_display_value(item, _CODEX_ITEM_TEXT_FIELDS, limits)
    if text:
        components.append(text)

    metadata = _codex_command_execution_metadata(record, item, limits)
    if metadata:
        components.append(metadata)

    for key in _CODEX_ITEM_OUTPUT_FIELDS:
        value = _display_field_value(item.get(key), limits)
        if value:
            components.append(f"{key}: {value}")

    if components:
        return " | ".join(components)
    return None


def _codex_web_search_detail(item: Mapping[object, object]) -> str | None:
    for value in _codex_web_search_detail_candidates(item):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _codex_web_search_detail_candidates(
    item: Mapping[object, object],
) -> list[object]:
    action = item.get("action")
    candidates: list[object] = []
    if isinstance(action, Mapping):
        candidates.append(action.get("query"))
        queries = action.get("queries")
        if isinstance(queries, list):
            candidates.extend(queries)
    candidates.append(item.get("query"))
    return candidates


def _codex_is_empty_web_search_event(item: Mapping[object, object]) -> bool:
    return _string_field(item, "type") == "web_search"


def _first_display_value(
    record: Mapping[object, object],
    keys: tuple[str, ...],
    limits: LogPresentationLimits,
) -> str | None:
    for key in keys:
        value = _display_field_value(record.get(key), limits)
        if value:
            return value
    return None


def _display_field_value(
    value: object,
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


def _string_field(
    record: Mapping[object, object],
    key: str,
) -> str | None:
    value = record.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
