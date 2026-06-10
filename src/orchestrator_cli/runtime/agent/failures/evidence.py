from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from itertools import chain

from orchestrator_cli.architecture.contracts import CommandResult, ProviderKind

from .formatting import clip_failure_summary, is_failure_hint
from .patterns import (
    ADVICE_BY_KIND,
    AUTH_OR_PERMISSION_PATTERNS,
    INITIAL_REQUEST_TOO_LARGE_PATTERNS,
    JSON_FAILURE_MARKERS,
    KIND_PRIORITY,
    MALFORMED_OUTPUT_PATTERNS,
    MAX_FAILURE_LINES,
    MAX_JSON_LINE_CHARS,
    MODEL_OR_CONFIG_PATTERNS,
    OUTPUT_LIMIT_PATTERNS,
    PROVIDER_ERROR_EVENT_TYPES,
    PROVIDER_SESSION_CONTEXT_PATTERNS,
    TOOL_ERROR_PATTERNS,
    TRANSPORT_ERROR_PATTERNS,
)
from .types import (
    FailureEvidence,
    FailureKind,
    FailurePhase,
    FailureSource,
    InvocationFailureSummary,
)


def failure_lines(result: CommandResult) -> Iterable[tuple[str, FailureSource]]:
    return chain(
        _stream_lines(result.iter_stderr_lines(), "stderr_json", "stderr_text"),
        _stream_lines(result.iter_stdout_lines(), "stdout_json", "stdout_text"),
    )


def collect_failure_evidence(
    provider_kind: ProviderKind,
    lines: Iterable[tuple[str, FailureSource]],
) -> tuple[list[FailureEvidence], list[tuple[str, FailureSource]], int]:
    evidence: list[FailureEvidence] = []
    candidate_lines: deque[tuple[str, FailureSource]] = deque()
    line_count = 0
    for sequence, (line, source) in enumerate(lines):
        line_count += 1
        item = _json_failure_evidence(provider_kind, line, source, sequence)
        if item is not None:
            evidence.append(item)
            continue
        item = _text_failure_evidence(provider_kind, line, source, sequence)
        if item is not None:
            evidence.append(item)
        candidate_lines.append((line, source))
        if len(candidate_lines) > MAX_FAILURE_LINES:
            candidate_lines.popleft()
    return evidence, list(candidate_lines), line_count


def _stream_lines(
    lines: Iterable[str], json_source: FailureSource, text_source: FailureSource
) -> Iterable[tuple[str, FailureSource]]:
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        source = json_source if line.startswith("{") else text_source
        yield line, source


def _json_failure_evidence(
    provider_kind: ProviderKind,
    line: str,
    source: FailureSource,
    sequence: int,
) -> FailureEvidence | None:
    if source not in {"stdout_json", "stderr_json"} or not _should_parse_json(line):
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not _payload_reports_error(payload):
        return None
    message = _json_failure_message(payload)
    if message is None:
        return None
    summary = _summary_for_message(provider_kind, message, source, False)
    priority = KIND_PRIORITY[summary.kind] + _json_event_priority(payload)
    return FailureEvidence(summary=summary, priority=priority, sequence=sequence)


def _text_failure_evidence(
    provider_kind: ProviderKind,
    line: str,
    source: FailureSource,
    sequence: int,
) -> FailureEvidence | None:
    if source not in {"stdout_text", "stderr_text"}:
        return None
    summary = _summary_for_message(provider_kind, line, source, False)
    if summary.kind == "unknown_provider_error" and not is_failure_hint(line):
        return None
    return FailureEvidence(
        summary=summary,
        priority=KIND_PRIORITY[summary.kind],
        sequence=sequence,
    )


def _should_parse_json(line: str) -> bool:
    if not line.startswith("{"):
        return False
    if len(line) <= MAX_JSON_LINE_CHARS:
        return any(marker in line for marker in JSON_FAILURE_MARKERS)
    return (
        '"type":"turn.failed"' in line
        or '"type": "turn.failed"' in line
        or '"is_error":true' in line
        or '"is_error": true' in line
    )


def _payload_reports_error(payload: dict[str, object]) -> bool:
    event_type = str(payload.get("type") or "").casefold()
    if any(marker in event_type for marker in PROVIDER_ERROR_EVENT_TYPES):
        return True
    if payload.get("is_error") is True:
        return True
    error = payload.get("error")
    return error is not None and error != "" and error is not False


def _json_failure_message(payload: dict[str, object]) -> str | None:
    error = payload.get("error")
    if isinstance(error, dict):
        message = _first_string(error, ("message", "detail", "type", "code", "status"))
        if message is not None:
            return message
    if isinstance(error, str) and error.strip():
        return error.strip()
    return _first_string(payload, ("message", "detail", "result", "status"))


def _first_string(payload: dict[object, object], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _json_event_priority(payload: dict[str, object]) -> int:
    event_type = str(payload.get("type") or "").casefold()
    if event_type == "turn.failed":
        return 500
    if "failed" in event_type:
        return 450
    if event_type == "error":
        return 400
    error = payload.get("error")
    if error is not None and error != "" and error is not False:
        return 350
    if payload.get("is_error") is True:
        return 300
    return 100


def _summary_for_message(
    provider_kind: ProviderKind,
    message: str,
    source: FailureSource,
    condensed: bool,
) -> InvocationFailureSummary:
    clipped_message, was_clipped = clip_failure_summary(message.strip())
    kind, phase = _classify_message(provider_kind, message)
    return InvocationFailureSummary(
        kind=kind,
        phase=phase,
        source=source,
        message=clipped_message,
        advice=ADVICE_BY_KIND[kind],
        condensed=condensed or was_clipped,
    )


def _classify_message(
    provider_kind: ProviderKind,
    message: str,
) -> tuple[FailureKind, FailurePhase]:
    text = message.casefold()
    if _contains_any(text, _quota_patterns(provider_kind)):
        return "quota_or_rate_limit", "provider_transport"
    if _contains_any(text, AUTH_OR_PERMISSION_PATTERNS):
        return "auth_or_permission", "provider_config"
    if _contains_any(text, MODEL_OR_CONFIG_PATTERNS):
        return "model_or_config_error", "provider_config"
    if _contains_any(text, INITIAL_REQUEST_TOO_LARGE_PATTERNS):
        return "initial_request_too_large", "initial_request"
    if _contains_any(text, PROVIDER_SESSION_CONTEXT_PATTERNS):
        return "provider_session_context_exhausted", "provider_session"
    if _contains_any(text, OUTPUT_LIMIT_PATTERNS):
        return "provider_output_limit_exceeded", "provider_output"
    if _contains_any(text, TOOL_ERROR_PATTERNS):
        return "provider_tool_error", "provider_tool"
    if _contains_any(text, TRANSPORT_ERROR_PATTERNS):
        return "provider_transport_error", "provider_transport"
    if _contains_any(text, MALFORMED_OUTPUT_PATTERNS):
        return "malformed_provider_output", "provider_output"
    if is_failure_hint(text):
        return "provider_error", "unknown"
    return "unknown_provider_error", "unknown"


def _quota_patterns(provider_kind: ProviderKind) -> tuple[str, ...]:
    if provider_kind == "gemini":
        return (
            "resource exhausted",
            "resource_exhausted",
            "resource-exhausted",
            "exhausted your capacity",
            "quota will reset",
            "quota exhausted",
            "rate limit exceeded",
            "too many requests",
            "429",
        )
    return (
        "usage limit reached",
        "usage limit exceeded",
        "resource exhausted",
        "resource_exhausted",
        "resource-exhausted",
        "quota reached",
        "quota exceeded",
        "rate limit reached",
        "rate limit exceeded",
        "too many requests",
        "429",
    )


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in text for pattern in patterns)
