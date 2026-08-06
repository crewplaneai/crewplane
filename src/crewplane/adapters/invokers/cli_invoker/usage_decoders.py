from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from crewplane.architecture.contracts import (
    CommandResult,
    ProviderTokenUsage,
    UsageDecodeResult,
)

from .machine_json import read_claude_model_usage
from .streaming import iter_stdout_lines, load_stdout_json


def decode_codex_usage(result: CommandResult) -> UsageDecodeResult:
    latest_tokens: ProviderTokenUsage | None = None
    malformed_error: str | None = None
    for line in iter_stdout_lines(result):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("type") != "turn.completed":
            continue
        usage = payload.get("usage")
        if usage is None:
            continue
        if not isinstance(usage, dict):
            malformed_error = "Malformed Codex usage report."
            continue
        try:
            tokens = _codex_tokens(usage)
        except _MalformedUsageError as exc:
            malformed_error = str(exc)
            continue
        if tokens.has_any_value():
            latest_tokens = tokens
    if latest_tokens is not None:
        return UsageDecodeResult(tokens=latest_tokens, valid_report_count=1)
    if malformed_error is not None:
        return UsageDecodeResult(error=malformed_error)
    return UsageDecodeResult()


def decode_claude_usage(result: CommandResult) -> UsageDecodeResult:
    payload, error = read_claude_model_usage(result)
    if error is not None:
        return UsageDecodeResult(error=error)
    if payload is None:
        return UsageDecodeResult()
    if not isinstance(payload, dict):
        return UsageDecodeResult(error="Malformed Claude modelUsage payload.")

    totals: ProviderTokenUsage | None = None
    valid_report_count = 0
    malformed_error: str | None = None
    for model_name, model_usage in payload.items():
        if not isinstance(model_name, str) or not isinstance(model_usage, dict):
            malformed_error = "Malformed Claude modelUsage payload."
            continue
        try:
            row_usage = _claude_row_usage(model_usage)
        except _MalformedUsageError as exc:
            malformed_error = str(exc)
            continue
        if not row_usage.has_any_value():
            continue
        totals = row_usage if totals is None else totals.add_exact(row_usage)
        valid_report_count += 1
    return _retained_usage_result(totals, valid_report_count, malformed_error)


def decode_gemini_usage(result: CommandResult) -> UsageDecodeResult:
    payload, error = load_stdout_json(result)
    if error is not None:
        return UsageDecodeResult(error=error)
    if payload is None:
        return UsageDecodeResult()
    stats = payload.get("stats")
    if not isinstance(stats, dict) or "models" not in stats:
        return UsageDecodeResult()
    models = stats["models"]
    rows = list(models.values()) if isinstance(models, dict) else models
    if not isinstance(rows, list):
        return UsageDecodeResult(error="Malformed Gemini stats.models payload.")

    totals: ProviderTokenUsage | None = None
    valid_report_count = 0
    malformed_error: str | None = None
    for row in rows:
        if not isinstance(row, dict):
            malformed_error = "Malformed Gemini model usage row."
            continue
        tokens = row.get("tokens")
        if tokens is None:
            continue
        if not isinstance(tokens, dict):
            malformed_error = "Malformed Gemini model tokens payload."
            continue
        try:
            row_usage = _gemini_row_usage(tokens)
        except _MalformedUsageError as exc:
            malformed_error = str(exc)
            continue
        if not row_usage.has_any_value():
            continue
        totals = row_usage if totals is None else totals.add_exact(row_usage)
        valid_report_count += 1
    return _retained_usage_result(totals, valid_report_count, malformed_error)


def decode_kilo_usage(result: CommandResult) -> UsageDecodeResult:
    totals: ProviderTokenUsage | None = None
    valid_report_count = 0
    malformed_error: str | None = None
    for line in iter_stdout_lines(result):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed_error = "Malformed Kilo JSON output."
            continue
        if not isinstance(event, dict) or event.get("type") != "step_finish":
            continue
        part = event.get("part")
        if not isinstance(part, dict) or "tokens" not in part:
            continue
        tokens = part["tokens"]
        if not isinstance(tokens, dict):
            malformed_error = "Malformed Kilo token payload."
            continue
        try:
            row_usage = _kilo_row_usage(tokens)
        except _MalformedUsageError as exc:
            malformed_error = str(exc)
            continue
        if not row_usage.has_any_value():
            continue
        totals = row_usage if totals is None else totals.add_exact(row_usage)
        valid_report_count += 1
    return _retained_usage_result(totals, valid_report_count, malformed_error)


def _retained_usage_result(
    tokens: ProviderTokenUsage | None,
    valid_report_count: int,
    error: str | None,
) -> UsageDecodeResult:
    retained_tokens = tokens if valid_report_count else None
    return UsageDecodeResult(
        tokens=retained_tokens,
        error=error,
        valid_report_count=valid_report_count,
    )


def _codex_tokens(
    payload: Mapping[str, object],
) -> ProviderTokenUsage:
    counters = _CounterReader("Codex", payload)
    input_tokens = counters.optional("input_tokens")
    cached_input = counters.optional("cached_input_tokens")
    output = counters.optional("output_tokens")
    reasoning = counters.optional("reasoning_output_tokens")
    total = None
    if input_tokens is not None and output is not None:
        total = input_tokens + output
    return ProviderTokenUsage(
        input=input_tokens,
        cached_input=cached_input,
        output=output,
        reasoning=reasoning,
        total=total,
    )


def _claude_row_usage(
    payload: Mapping[str, object],
) -> ProviderTokenUsage:
    counters = _CounterReader("Claude", payload)
    input_tokens = counters.optional("inputTokens")
    cache_read = counters.optional("cacheReadInputTokens")
    cache_write = counters.optional("cacheCreationInputTokens")
    output = counters.optional("outputTokens")
    normalized_input = _sum_present(input_tokens, cache_read, cache_write)
    total = (
        normalized_input + output
        if normalized_input is not None and output is not None
        else None
    )
    return ProviderTokenUsage(
        input=normalized_input,
        cached_input=cache_read,
        cache_write=cache_write,
        output=output,
        total=total,
    )


def _gemini_row_usage(
    payload: Mapping[str, object],
) -> ProviderTokenUsage:
    counters = _CounterReader("Gemini", payload)
    prompt = counters.optional("prompt")
    cached = counters.optional("cached")
    candidates = counters.optional("candidates")
    thoughts = counters.optional("thoughts")
    tool = counters.optional("tool")
    total = counters.optional("total")
    output = _sum_present(candidates, thoughts, tool)
    return ProviderTokenUsage(
        input=prompt,
        cached_input=cached,
        output=output,
        reasoning=thoughts,
        total=total,
    )


def _kilo_row_usage(
    payload: Mapping[str, object],
) -> ProviderTokenUsage:
    counters = _CounterReader("Kilo", payload)
    input_tokens = counters.optional("input")
    output = counters.optional("output")
    reasoning = counters.optional("reasoning")
    cache_read, cache_write = _kilo_cache_counters(payload)
    normalized_input = _sum_present(input_tokens, cache_read, cache_write)
    normalized_output = _sum_present(output, reasoning)
    total = _complete_sum(input_tokens, output, reasoning, cache_read, cache_write)
    return ProviderTokenUsage(
        input=normalized_input,
        cached_input=cache_read,
        cache_write=cache_write,
        output=normalized_output,
        reasoning=reasoning,
        total=total,
    )


def _kilo_cache_counters(
    payload: Mapping[str, object],
) -> tuple[int | None, int | None]:
    cache = payload.get("cache")
    if cache is None:
        cache = {}
    if not isinstance(cache, dict):
        raise _MalformedUsageError("Malformed Kilo cache payload.")
    counters = _CounterReader("Kilo", cache)
    return counters.optional("read"), counters.optional("write")


class _MalformedUsageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _CounterReader:
    provider: str
    payload: Mapping[str, object]

    def optional(self, key: str) -> int | None:
        value = self.payload.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _MalformedUsageError(
                f"Malformed {self.provider} usage: "
                f"{key} must be a non-negative integer."
            )
        return value


def _sum_present(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _complete_sum(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if len(present) == len(values) else None
