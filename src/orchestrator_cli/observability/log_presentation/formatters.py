from __future__ import annotations

import json
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from time import time
from typing import Any

from orchestrator_cli.architecture.contracts import (
    LogPresentationDescriptor,
    validate_log_presentation_descriptor,
)
from orchestrator_cli.observability.events.types import InvocationStatus

from .json_extract import exceeds_json_depth, render_json_object, render_json_record
from .limits import DEFAULT_LIMITS, LogPresentationLimits
from .models import (
    LogPresentationNotice,
    LogPresentationRequest,
    LogPresentationSnapshot,
    LogReadResult,
)
from .sanitize import decode_sanitized, sanitize_line, sanitize_lines
from .tail import read_bounded_tail, read_latest_attempt_body
from .throttle import JSON_OBJECT_THROTTLE

_STDERR_PREFIX = "[stderr] "


@dataclass(frozen=True)
class _JsonObjectRecovery:
    parsed: Any
    diagnostics: tuple[str, ...] = ()


def format_log_file(
    log_path: Path,
    descriptor: LogPresentationDescriptor | object,
    line_budget: int,
    invocation_status: InvocationStatus = "running",
    wall_time_now: float | None = None,
    limits: LogPresentationLimits = DEFAULT_LIMITS,
) -> LogPresentationSnapshot:
    validated = validate_log_presentation_descriptor(descriptor)
    request = LogPresentationRequest(
        log_path=log_path,
        presentation_format=validated.format,
        presentation_profile=validated.profile,
        line_budget=max(1, line_budget),
        invocation_status=invocation_status,
        wall_time_now=time() if wall_time_now is None else wall_time_now,
        limits=limits,
    )
    match request.presentation_format:
        case "plain":
            return format_plain(request)
        case "json_lines":
            return format_json_lines(request)
        case "json_object":
            return format_json_object(request)


def format_plain(request: LogPresentationRequest) -> LogPresentationSnapshot:
    result = read_bounded_tail(request.log_path, request.wall_time_now, request.limits)
    if result is None:
        return unavailable_snapshot()
    text = decode_sanitized(result.body)
    lines = text.splitlines()
    if result.started_mid_line and lines:
        lines = lines[1:]
    sanitized = sanitize_lines(lines[-request.line_budget :], request.limits)
    return LogPresentationSnapshot(
        size_bytes=result.size_bytes,
        updated_age_seconds=result.updated_age_seconds,
        lines=sanitized,
        truncated=result.truncated,
    )


def format_json_lines(request: LogPresentationRequest) -> LogPresentationSnapshot:
    result = read_bounded_tail(request.log_path, request.wall_time_now, request.limits)
    if result is None:
        return unavailable_snapshot()

    lines = decode_sanitized(result.body).splitlines()
    if result.started_mid_line and lines:
        lines = lines[1:]

    rendered: list[str] = []
    notices: list[LogPresentationNotice] = []
    for line in lines:
        if not line.strip():
            continue
        payload = _strip_stderr_prefix(line)
        if len(payload.encode("utf-8")) > request.limits.max_jsonl_record_bytes:
            notices.append(warning_notice("Skipped oversized JSONL record."))
            continue
        try:
            record = json.loads(payload)
        except RecursionError:
            notices.append(warning_notice("Skipped deeply nested JSONL record."))
            continue
        except (TypeError, ValueError):
            record = None
        if record is None:
            if line.startswith(_STDERR_PREFIX):
                rendered.append(sanitize_line(f"stderr: {payload}", request.limits))
            else:
                rendered.append(sanitize_line(line, request.limits))
            continue
        if exceeds_json_depth(record, request.limits.max_json_depth):
            notices.append(warning_notice("Skipped deeply nested JSONL record."))
            continue
        rendered.extend(
            render_json_record(record, request.presentation_profile, request.limits)
        )

    return LogPresentationSnapshot(
        size_bytes=result.size_bytes,
        updated_age_seconds=result.updated_age_seconds,
        lines=tuple(rendered[-request.line_budget :]),
        notices=tuple(notices[-request.line_budget :]),
        truncated=result.truncated,
    )


def format_json_object(request: LogPresentationRequest) -> LogPresentationSnapshot:
    result = read_latest_attempt_body(
        request.log_path,
        request.wall_time_now,
        request.limits,
        max_bytes=request.limits.max_json_object_parse_bytes,
    )
    if result is None:
        return unavailable_snapshot()

    raw_text = decode_sanitized(result.body)
    body = _strip_stderr_prefixes(raw_text)
    if result.truncated:
        JSON_OBJECT_THROTTLE.clear_path(request.log_path)
        return fallback_text_snapshot(
            result,
            request,
            [warning_notice("Structured provider log exceeds display parse limit.")],
        )

    if not JSON_OBJECT_THROTTLE.should_parse(
        request.log_path,
        request.invocation_status,
        result.size_bytes,
        request.limits,
    ):
        return fallback_text_snapshot(
            result,
            request,
            [info_notice("Waiting for complete JSON object...")],
        )

    diagnostics: tuple[str, ...] = ()
    try:
        parsed = json.loads(body)
    except RecursionError:
        JSON_OBJECT_THROTTLE.clear_path(request.log_path)
        return fallback_text_snapshot(
            result,
            request,
            [warning_notice("Structured provider log is too deeply nested.")],
        )
    except JSONDecodeError as exc:
        recovered = _recover_json_object(raw_text, request.presentation_profile)
        if recovered is None:
            if _looks_incomplete_json_error(exc):
                JSON_OBJECT_THROTTLE.mark_incomplete(
                    request.log_path,
                    result.size_bytes,
                )
            else:
                JSON_OBJECT_THROTTLE.clear_path(request.log_path)
            return fallback_text_snapshot(
                result,
                request,
                [warning_notice("Could not parse structured provider log.")],
            )
        parsed = recovered.parsed
        diagnostics = recovered.diagnostics
    except ValueError:
        JSON_OBJECT_THROTTLE.clear_path(request.log_path)
        return fallback_text_snapshot(
            result,
            request,
            [warning_notice("Could not parse structured provider log.")],
        )

    JSON_OBJECT_THROTTLE.clear_path(request.log_path)
    notices: list[LogPresentationNotice] = []
    if result.truncated:
        notices.append(warning_notice("Structured log was read from a bounded tail."))
    if exceeds_json_depth(parsed, request.limits.max_json_depth):
        return fallback_text_snapshot(
            result,
            request,
            [warning_notice("Structured provider log is too deeply nested.")],
        )

    rendered = [
        *_diagnostic_display_lines(diagnostics, request.limits),
        *render_json_object(parsed, request.presentation_profile, request.limits),
    ]
    return LogPresentationSnapshot(
        size_bytes=result.size_bytes,
        updated_age_seconds=result.updated_age_seconds,
        lines=tuple(rendered[: request.line_budget]),
        notices=tuple(notices),
        truncated=result.truncated,
    )


def fallback_text_snapshot(
    result: LogReadResult,
    request: LogPresentationRequest,
    notices: list[LogPresentationNotice],
) -> LogPresentationSnapshot:
    text = decode_sanitized(result.body)
    lines = sanitize_lines(text.splitlines()[-request.line_budget :], request.limits)
    return LogPresentationSnapshot(
        size_bytes=result.size_bytes,
        updated_age_seconds=result.updated_age_seconds,
        lines=lines,
        notices=tuple(notices),
        truncated=result.truncated,
    )


def unavailable_snapshot() -> LogPresentationSnapshot:
    return LogPresentationSnapshot(
        size_bytes=0,
        updated_age_seconds=None,
        lines=(),
        notices=(warning_notice("Log file unavailable."),),
    )


def warning_notice(message: str) -> LogPresentationNotice:
    return LogPresentationNotice(level="warning", message=sanitize_line(message))


def info_notice(message: str) -> LogPresentationNotice:
    return LogPresentationNotice(level="info", message=sanitize_line(message))


def _strip_stderr_prefix(line: str) -> str:
    return line[len(_STDERR_PREFIX) :] if line.startswith(_STDERR_PREFIX) else line


def _strip_stderr_prefixes(text: str) -> str:
    return "\n".join(_strip_stderr_prefix(line) for line in text.splitlines())


def _recover_json_object(text: str, profile: str) -> _JsonObjectRecovery | None:
    if profile != "claude":
        return None
    stripped_lines = [_strip_stderr_prefix(line) for line in text.splitlines()]
    recovered = _recover_claude_json_with_outer_diagnostics(stripped_lines)
    if recovered is not None:
        return recovered
    return _recover_claude_json_with_inner_diagnostics(text.splitlines())


def _recover_claude_json_with_outer_diagnostics(
    stripped_lines: list[str],
) -> _JsonObjectRecovery | None:
    text = "\n".join(stripped_lines)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    parsed = _parse_claude_shaped_json(text[start : end + 1])
    if parsed is None:
        return None
    diagnostics = [
        line.strip()
        for line in (f"{text[:start]}\n{text[end + 1 :]}")
        .replace("\r", "\n")
        .splitlines()
        if line.strip()
    ]
    return _JsonObjectRecovery(parsed=parsed, diagnostics=tuple(diagnostics))


def _recover_claude_json_with_inner_diagnostics(
    raw_lines: list[str],
) -> _JsonObjectRecovery | None:
    if not raw_lines:
        return None
    stripped_lines = [_strip_stderr_prefix(line) for line in raw_lines]
    start_line = _first_line_containing(stripped_lines, "{")
    end_line = _last_line_containing(stripped_lines, "}")
    if start_line is None or end_line is None or end_line <= start_line:
        return None

    kept_lines: list[str] = []
    diagnostics: list[str] = []
    for raw_line, stripped_line in zip(
        raw_lines[start_line : end_line + 1],
        stripped_lines[start_line : end_line + 1],
        strict=True,
    ):
        if raw_line.startswith(_STDERR_PREFIX) and not _looks_like_json_fragment(
            stripped_line
        ):
            diagnostics.append(stripped_line.strip())
            continue
        kept_lines.append(stripped_line)
    if not diagnostics:
        return None

    parsed = _parse_claude_shaped_json("\n".join(kept_lines))
    if parsed is None:
        return None
    outer_diagnostics = [
        line.strip()
        for line in (
            *stripped_lines[:start_line],
            *stripped_lines[end_line + 1 :],
        )
        if line.strip()
    ]
    return _JsonObjectRecovery(
        parsed=parsed,
        diagnostics=tuple([*outer_diagnostics, *diagnostics]),
    )


def _parse_claude_shaped_json(candidate: str) -> Any | None:
    try:
        parsed = json.loads(candidate)
    except (RecursionError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if not any(key in parsed for key in ("result", "error", "usage")):
        return None
    return parsed


def _diagnostic_display_lines(
    diagnostics: tuple[str, ...],
    limits: LogPresentationLimits,
) -> list[str]:
    return [
        sanitize_line(f"stderr: {line}", limits)
        for line in diagnostics[: limits.max_mixed_stream_diagnostic_lines]
        if line.strip()
    ]


def _looks_like_json_fragment(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith(("{", "}", "[", "]", '"', ",", ":"))


def _first_line_containing(lines: list[str], marker: str) -> int | None:
    for index, line in enumerate(lines):
        if marker in line:
            return index
    return None


def _last_line_containing(lines: list[str], marker: str) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if marker in lines[index]:
            return index
    return None


def _looks_incomplete_json_error(exc: JSONDecodeError) -> bool:
    return exc.pos >= max(0, len(exc.doc) - 2)
