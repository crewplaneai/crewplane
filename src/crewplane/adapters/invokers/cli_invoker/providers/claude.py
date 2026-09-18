from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from functools import partial
from pathlib import Path

from crewplane.architecture.contracts import (
    CommandResult,
    ProviderKind,
    ProviderTokenUsage,
    UsageDecodeResult,
)

from ..capability import CliCommand, CliInvocationRequest, CliProviderCapability
from ..claude_json import extract_claude_output, read_claude_model_usage
from ..commands import build_standard_command
from ..failures.classifier import classify_generic_failure
from ..quota.classifier import classify_generic_quota
from ..usage_decoders import (
    CounterReader,
    UsageAccumulator,
    sum_present,
)
from ..validation import reasoning_command_context

CLAUDE_REASONING_ENV = "CLAUDE_CODE_EFFORT_LEVEL"
MAX_CAPTURED_CLAUDE_USAGE_BYTES = 1024 * 1024
QUOTA_HINTS = (
    "usage limit reached",
    "too many requests",
)


def decode_claude_usage(result: CommandResult) -> UsageDecodeResult:
    payload, error = read_claude_model_usage(result, MAX_CAPTURED_CLAUDE_USAGE_BYTES)
    if error is not None:
        return UsageDecodeResult(error=error)
    if payload is None:
        return UsageDecodeResult()
    if not isinstance(payload, dict):
        return UsageDecodeResult(error="Malformed Claude modelUsage payload.")

    accumulator = UsageAccumulator()
    for model_name, model_usage in payload.items():
        if not isinstance(model_name, str) or not isinstance(model_usage, dict):
            accumulator.record_error("Malformed Claude modelUsage payload.")
            continue
        accumulator.decode_and_record(_claude_row_usage, model_usage)
    return accumulator.result()


def _claude_row_usage(
    payload: Mapping[str, object],
) -> ProviderTokenUsage:
    counters = CounterReader("Claude", payload)
    input_tokens = counters.optional("inputTokens")
    cache_read = counters.optional("cacheReadInputTokens")
    cache_write = counters.optional("cacheCreationInputTokens")
    output = counters.optional("outputTokens")
    normalized_input = sum_present(input_tokens, cache_read, cache_write)
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


def _reject_claude_reasoning_conflict(
    tokens: Sequence[str],
    working_directory: Path | None,
) -> None:
    for option, value in _iter_claude_reasoning_options(tokens):
        if option == "--effort":
            raise ValueError("--effort conflicts with the workflow reasoning request.")
        _reject_claude_settings_reasoning_conflict(value, working_directory)


def _iter_claude_reasoning_options(
    tokens: Sequence[str],
) -> Iterator[tuple[str, str]]:
    remaining = iter(tokens)
    for token in remaining:
        if token == "--":
            return
        option, separator, value = token.partition("=")
        if option not in {"--effort", "--settings"}:
            continue
        if separator:
            if option == "--settings" and not value:
                raise ValueError("--settings requires a JSON object or file path.")
        else:
            next_value = next(remaining, None)
            if next_value is None or next_value == "--":
                requirement = (
                    "a value" if option == "--effort" else "a JSON object or file path"
                )
                raise ValueError(f"{option} requires {requirement}.")
            value = next_value
        yield option, value


def _reject_claude_settings_reasoning_conflict(
    settings_value: str,
    working_directory: Path | None,
) -> None:
    settings = _load_claude_settings(settings_value, working_directory)
    if _is_nonblank_settings_value(settings.get("effortLevel")):
        raise ValueError(
            "--settings effortLevel conflicts with the workflow reasoning request."
        )
    settings_environment = settings.get("env")
    if not isinstance(settings_environment, Mapping):
        return
    if _is_nonblank_settings_value(settings_environment.get(CLAUDE_REASONING_ENV)):
        raise ValueError(
            f"--settings {CLAUDE_REASONING_ENV} conflicts with the workflow "
            "reasoning request."
        )


def _load_claude_settings(
    settings_value: str,
    working_directory: Path | None,
) -> Mapping[str, object]:
    raw_settings = (
        settings_value
        if settings_value.lstrip().startswith("{")
        else _read_claude_settings_file(settings_value, working_directory)
    )
    return _parse_claude_settings(raw_settings)


def _read_claude_settings_file(
    settings_value: str,
    working_directory: Path | None,
) -> str:
    settings_path = Path(settings_value).expanduser()
    if not settings_path.is_absolute():
        base_directory = Path.cwd() if working_directory is None else working_directory
        settings_path = base_directory / settings_path
    try:
        return settings_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(
            "Cannot validate the Claude --settings file against the workflow "
            "reasoning request."
        ) from exc


def _parse_claude_settings(raw_settings: str) -> Mapping[str, object]:
    try:
        settings: object = json.loads(raw_settings)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Claude --settings must contain a valid JSON object when workflow "
            "reasoning is requested."
        ) from exc
    if not isinstance(settings, dict):
        raise ValueError(
            "Claude --settings must contain a JSON object when workflow reasoning "
            "is requested."
        )
    return settings


def _is_nonblank_settings_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def validate_claude_request(request: CliInvocationRequest) -> None:
    if request.requested_reasoning is None:
        return
    context = reasoning_command_context(request, CLAUDE_REASONING_ENV)
    _reject_claude_reasoning_conflict(
        context.command_arguments, request.working_directory
    )
    _reject_claude_reasoning_conflict(
        request.config.extra_args, request.working_directory
    )
    value = context.tracked_environment_value
    if value and value.strip():
        raise ValueError(
            f"{CLAUDE_REASONING_ENV} conflicts with the workflow reasoning request."
        )


def build_claude_command(request: CliInvocationRequest, prompt: str) -> CliCommand:
    reasoning_args = (
        ()
        if request.requested_reasoning is None
        else ("--effort", request.requested_reasoning)
    )
    return build_standard_command(
        request, prompt, ("--output-format", "json"), reasoning_args
    )


CLAUDE = CliProviderCapability(
    provider_kind=ProviderKind.CLAUDE,
    validate_request=validate_claude_request,
    build_command=build_claude_command,
    output_extractor=extract_claude_output,
    usage_decoder=decode_claude_usage,
    quota_classifier=partial(classify_generic_quota, family_hints=QUOTA_HINTS),
    failure_classifier=classify_generic_failure,
    log_presentation_format="json_object",
    log_presentation_profile="claude",
)
