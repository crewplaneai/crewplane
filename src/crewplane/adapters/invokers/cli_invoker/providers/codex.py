from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path

from crewplane.architecture.contracts import (
    CommandResult,
    OneShotFailureRetryPolicy,
    OutputExtractionResult,
    ProviderKind,
    ProviderTokenUsage,
    UsageDecodeResult,
)
from crewplane.core.file_text import (
    path_decoded_character_count,
    path_has_non_whitespace_text,
)

from ..capability import CliCommand, CliInvocationRequest, CliProviderCapability
from ..commands import build_standard_command
from ..failures.classifier import classify_generic_failure
from ..quota.classifier import classify_generic_quota
from ..streaming import iter_stdout_lines
from ..usage_decoders import (
    CounterReader,
    MalformedUsageError,
)
from ..validation import reasoning_command_context


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
        except MalformedUsageError as exc:
            malformed_error = str(exc)
            continue
        if tokens.has_any_value():
            latest_tokens = tokens
    if latest_tokens is not None:
        return UsageDecodeResult(tokens=latest_tokens, valid_report_count=1)
    if malformed_error is not None:
        return UsageDecodeResult(error=malformed_error)
    return UsageDecodeResult()


def _codex_tokens(
    payload: Mapping[str, object],
) -> ProviderTokenUsage:
    counters = CounterReader("Codex", payload)
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


def extract_codex_output(
    result: CommandResult,  # noqa: ARG001 - Required by OutputExtractor callback.
    structured_output_file: Path | None,
) -> OutputExtractionResult:
    if structured_output_file is None or not structured_output_file.exists():
        return _missing_output()
    if not path_has_non_whitespace_text(structured_output_file):
        return _missing_output()
    return OutputExtractionResult(
        output_text="",
        output_extraction_status="success",
        output_path=structured_output_file,
        output_char_count=path_decoded_character_count(structured_output_file),
    )


def _missing_output() -> OutputExtractionResult:
    return OutputExtractionResult(output_text="", output_extraction_status="missing")


CODEX_REASONING_KEY = "model_reasoning_effort"


def _reject_codex_reasoning_conflict(tokens: Sequence[str]) -> None:
    for assignment in _codex_config_assignments(tokens):
        key = assignment.partition("=")[0].strip()
        if key == CODEX_REASONING_KEY:
            raise ValueError(
                f"{CODEX_REASONING_KEY} conflicts with the workflow reasoning request."
            )


def _codex_config_assignments(tokens: Sequence[str]) -> tuple[str, ...]:
    assignments: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        assignment, consumed = _codex_config_assignment(tokens, index)
        if assignment is not None:
            assignments.append(assignment)
        index += consumed
    return tuple(assignments)


def _codex_config_assignment(
    tokens: Sequence[str],
    index: int,
) -> tuple[str | None, int]:
    token = tokens[index]
    if token in {"--config", "-c"}:
        if index + 1 >= len(tokens) or tokens[index + 1] == "--":
            raise ValueError(f"{token} requires a TOML assignment.")
        return _require_toml_assignment(tokens[index + 1], token), 2
    if token.startswith("--config="):
        return _require_toml_assignment(token.removeprefix("--config="), "--config"), 1
    if token.startswith("-c="):
        return _require_toml_assignment(token.removeprefix("-c="), "-c"), 1
    if token.startswith("-c") and token != "-c":
        return _require_toml_assignment(token[2:], "-c"), 1
    return None, 1


def _require_toml_assignment(value: str, option: str) -> str:
    if "=" not in value or not value.partition("=")[0].strip():
        raise ValueError(f"{option} requires a TOML key=value assignment.")
    return value


CODEX_MODEL_CAPACITY_MESSAGE = (
    "Selected model is at capacity. Please try a different model."
)


CODEX_MODEL_CAPACITY_RETRY_DELAY_SECONDS = 5.0


CODEX_MODEL_CAPACITY_RETRY_POLICY = OneShotFailureRetryPolicy(
    output_contains=(CODEX_MODEL_CAPACITY_MESSAGE,),
    wait_seconds=CODEX_MODEL_CAPACITY_RETRY_DELAY_SECONDS,
    reason="codex_model_capacity",
    notice_message=(
        f'Codex reported "{CODEX_MODEL_CAPACITY_MESSAGE}" '
        "Crewplane will retry in five seconds (built-in attempt 1/1)."
    ),
)


def validate_codex_request(request: CliInvocationRequest) -> None:
    if request.requested_reasoning is None:
        return
    context = reasoning_command_context(request)
    _reject_codex_reasoning_conflict(context.command_arguments)
    _reject_codex_reasoning_conflict(request.config.extra_args)


def build_codex_command(request: CliInvocationRequest, prompt: str) -> CliCommand:
    file_descriptor, temp_path = tempfile.mkstemp(
        prefix="crewplane-codex-", suffix=".last-message.txt"
    )
    output_path = Path(temp_path)
    try:
        os.close(file_descriptor)
        output_path.unlink(missing_ok=True)
        reasoning_args = (
            ()
            if request.requested_reasoning is None
            else ("--config", f'{CODEX_REASONING_KEY}="{request.requested_reasoning}"')
        )
        command = build_standard_command(
            request,
            prompt,
            ("--json", "--output-last-message", str(output_path)),
            reasoning_args,
        )
        return CliCommand(command.cmd, command.stdin_data, output_path)
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise


QUOTA_HINTS = (
    "usage limit exceeded",
    "usage limit",
    "rate limit",
    "too many requests",
    "try again in",
    "retry after",
    "reset after",
    "reset at",
    "resetsat",
)

CODEX = CliProviderCapability(
    provider_kind=ProviderKind.CODEX,
    validate_request=validate_codex_request,
    build_command=build_codex_command,
    output_extractor=extract_codex_output,
    usage_decoder=decode_codex_usage,
    quota_classifier=partial(classify_generic_quota, family_hints=QUOTA_HINTS),
    failure_classifier=classify_generic_failure,
    log_presentation_format="json_lines",
    log_presentation_profile="codex",
    one_shot_failure_retry=CODEX_MODEL_CAPACITY_RETRY_POLICY,
)
