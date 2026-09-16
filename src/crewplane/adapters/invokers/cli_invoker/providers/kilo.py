from __future__ import annotations

import json
from collections.abc import Mapping
from functools import partial
from pathlib import Path

from crewplane.architecture.contracts import (
    CommandResult,
    OutputExtractionResult,
    ProviderKind,
    ProviderTokenUsage,
    UsageDecodeResult,
)

from ..capability import CliProviderCapability
from ..commands import build_standard_command
from ..failures.classifier import classify_generic_failure
from ..quota.classifier import classify_generic_quota
from ..streaming import iter_stdout_json_objects, iter_stdout_lines
from ..usage_decoders import (
    CounterReader,
    MalformedUsageError,
    UsageAccumulator,
    complete_sum,
    sum_present,
)
from ..validation import reject_unsupported_reasoning


def decode_kilo_usage(result: CommandResult) -> UsageDecodeResult:
    accumulator = UsageAccumulator()
    for line in iter_stdout_lines(result):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            accumulator.record_error("Malformed Kilo JSON output.")
            continue
        if not isinstance(event, dict) or event.get("type") != "step_finish":
            continue
        part = event.get("part")
        if not isinstance(part, dict) or "tokens" not in part:
            continue
        tokens = part["tokens"]
        if not isinstance(tokens, dict):
            accumulator.record_error("Malformed Kilo token payload.")
            continue
        accumulator.decode_and_record(_kilo_row_usage, tokens)
    return accumulator.result()


def _kilo_row_usage(
    payload: Mapping[str, object],
) -> ProviderTokenUsage:
    counters = CounterReader("Kilo", payload)
    input_tokens = counters.optional("input")
    output = counters.optional("output")
    reasoning = counters.optional("reasoning")
    cache_read, cache_write = _kilo_cache_counters(payload)
    normalized_input = sum_present(input_tokens, cache_read, cache_write)
    normalized_output = sum_present(output, reasoning)
    total = complete_sum(input_tokens, output, reasoning, cache_read, cache_write)
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
        raise MalformedUsageError("Malformed Kilo cache payload.")
    counters = CounterReader("Kilo", cache)
    return counters.optional("read"), counters.optional("write")


def extract_kilo_output(
    result: CommandResult,
    structured_output_file: Path | None,  # noqa: ARG001 - Required by OutputExtractor callback.
) -> OutputExtractionResult:
    text_parts: list[str] = []
    for event in iter_stdout_json_objects(result):
        if event is None:
            return _malformed_output()
        text = _kilo_text_event(event)
        if text is not None:
            text = text.strip()
            if text:
                text_parts.append(text)
    if not text_parts:
        return _missing_output()
    output_text = "\n".join(text_parts) + "\n"
    return OutputExtractionResult(
        output_text=output_text,
        output_extraction_status="success",
        output_char_count=len(output_text),
    )


def _missing_output() -> OutputExtractionResult:
    return OutputExtractionResult(output_text="", output_extraction_status="missing")


def _malformed_output() -> OutputExtractionResult:
    return OutputExtractionResult(
        output_text="",
        output_extraction_status="malformed",
    )


def _kilo_text_event(event: Mapping[str, object]) -> str | None:
    if event.get("type") != "text":
        return None
    part = event.get("part")
    text = part.get("text") if isinstance(part, dict) else event.get("text")
    return text if isinstance(text, str) else None


QUOTA_HINTS = (
    "rate limit",
    "quota",
    "too many requests",
    "429",
    "retry after",
    "reset after",
    "try again in",
)

KILO = CliProviderCapability(
    provider_kind=ProviderKind.KILO,
    validate_request=reject_unsupported_reasoning,
    build_command=partial(build_standard_command, structured_args=("--format", "json")),
    output_extractor=extract_kilo_output,
    usage_decoder=decode_kilo_usage,
    quota_classifier=partial(classify_generic_quota, family_hints=QUOTA_HINTS),
    failure_classifier=classify_generic_failure,
    log_presentation_format="json_lines",
    log_presentation_profile="kilo",
)
