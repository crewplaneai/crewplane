from __future__ import annotations

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
from ..streaming import (
    load_stdout_json,
    malformed_output,
    missing_output,
)
from ..usage_decoders import (
    CounterReader,
    MalformedUsageError,
    UsageAccumulator,
    sum_present,
)
from ..validation import reject_unsupported_reasoning

QUOTA_HINTS = (
    "exhausted your capacity",
    "resource exhausted",
    "no capacity available",
    "retryable quota error",
    "max attempts reached",
    "rate limit exceeded",
    "too many requests",
    "429",
    "quota will reset",
    "quota exhausted",
)
FAILURE_QUOTA_PATTERNS = (
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


def decode_gemini_usage(result: CommandResult) -> UsageDecodeResult:
    payload, error = load_stdout_json(result)
    if error is not None:
        return UsageDecodeResult(error=error)
    if payload is None:
        return UsageDecodeResult()
    try:
        rows = _gemini_model_rows(payload)
    except MalformedUsageError as exc:
        return UsageDecodeResult(error=str(exc))
    accumulator = UsageAccumulator()
    for row in rows:
        _record_gemini_model_usage(row, accumulator)
    return accumulator.result()


def _gemini_model_rows(payload: Mapping[str, object]) -> list[object]:
    stats = payload.get("stats")
    if not isinstance(stats, dict) or "models" not in stats:
        return []
    models = stats["models"]
    rows = list(models.values()) if isinstance(models, dict) else models
    if not isinstance(rows, list):
        raise MalformedUsageError("Malformed Gemini stats.models payload.")
    return rows


def _record_gemini_model_usage(row: object, accumulator: UsageAccumulator) -> None:
    if not isinstance(row, dict):
        accumulator.record_error("Malformed Gemini model usage row.")
        return
    tokens = row.get("tokens")
    if tokens is None:
        return
    if not isinstance(tokens, dict):
        accumulator.record_error("Malformed Gemini model tokens payload.")
        return
    accumulator.decode_and_record(_gemini_row_usage, tokens)


def _gemini_row_usage(
    payload: Mapping[str, object],
) -> ProviderTokenUsage:
    counters = CounterReader("Gemini", payload)
    prompt = counters.optional("prompt")
    cached = counters.optional("cached")
    candidates = counters.optional("candidates")
    thoughts = counters.optional("thoughts")
    tool = counters.optional("tool")
    total = counters.optional("total")
    output = sum_present(candidates, thoughts, tool)
    return ProviderTokenUsage(
        input=prompt,
        cached_input=cached,
        output=output,
        reasoning=thoughts,
        total=total,
    )


def extract_gemini_output(
    result: CommandResult,
    structured_output_file: Path | None,  # noqa: ARG001 - Required by OutputExtractor callback.
) -> OutputExtractionResult:
    payload, error = load_stdout_json(result)
    if error is not None:
        return malformed_output()
    if payload is None or "response" not in payload:
        return missing_output()
    response = payload["response"]
    if not isinstance(response, str):
        return malformed_output()
    if not response.strip():
        return missing_output()
    return OutputExtractionResult(
        output_text=response,
        output_extraction_status="success",
        output_char_count=len(response),
    )


GEMINI = CliProviderCapability(
    provider_kind=ProviderKind.GEMINI,
    validate_request=reject_unsupported_reasoning,
    build_command=partial(
        build_standard_command, structured_args=("--output-format", "json")
    ),
    output_extractor=extract_gemini_output,
    usage_decoder=decode_gemini_usage,
    quota_classifier=partial(classify_generic_quota, family_hints=QUOTA_HINTS),
    failure_classifier=partial(
        classify_generic_failure, quota_patterns=FAILURE_QUOTA_PATTERNS
    ),
    log_presentation_format="json_object",
    log_presentation_profile="gemini",
    supports_output_idle_timeout=False,
)
