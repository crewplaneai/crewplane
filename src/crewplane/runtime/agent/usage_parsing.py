from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import cast

from crewplane.architecture.contracts import CommandResult, JsonObject, JsonValue

from .usage_types import (
    TOKEN_BUCKETS,
    TOKEN_KEY_ALIASES,
    ParsedProviderUsage,
    ProviderTokenUsage,
    TokenBucket,
    UsageParser,
)


def parse_provider_usage(
    parser: UsageParser,
    stdout_text: str,
    stderr_text: str,
) -> ParsedProviderUsage:
    return parse_provider_usage_from_lines(
        parser=parser,
        stdout_lines=stdout_text.splitlines(),
        stderr_lines=stderr_text.splitlines(),
    )


def parse_provider_usage_from_result(
    parser: UsageParser,
    result: CommandResult,
) -> ParsedProviderUsage:
    return parse_provider_usage_from_lines(
        parser=parser,
        stdout_lines=result.iter_stdout_lines(),
        stderr_lines=result.iter_stderr_lines(),
    )


def parse_provider_usage_from_lines(
    parser: UsageParser,
    stdout_lines: Iterable[str],
    stderr_lines: Iterable[str],
) -> ParsedProviderUsage:
    if parser == "codex":
        return parse_codex_usage(stdout_lines, stderr_lines)
    if parser == "claude":
        payload, error = load_structured_output_payload_from_lines(
            stdout_lines,
            stderr_lines,
        )
        if error is not None:
            return ParsedProviderUsage(status="malformed", error=error)
        if payload is None:
            return ParsedProviderUsage(status="none")
        usage_payload = payload.get("usage")
        if usage_payload is None:
            return ParsedProviderUsage(status="none")
        return parse_usage_mapping(usage_payload)
    return ParsedProviderUsage(status="none")


def parse_codex_usage(
    stdout_lines: Iterable[str],
    stderr_lines: Iterable[str],
) -> ParsedProviderUsage:
    latest_tokens: ProviderTokenUsage | None = None
    malformed_error: str | None = None
    saw_usage_candidate = False
    for text in (stdout_lines, stderr_lines):
        for line in text:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            candidate = find_usage_mapping(payload)
            if candidate is None:
                continue
            saw_usage_candidate = True
            parsed_usage = parse_usage_mapping(candidate)
            if parsed_usage.status == "malformed":
                malformed_error = parsed_usage.error
                continue
            if parsed_usage.tokens is not None:
                latest_tokens = parsed_usage.tokens
    if latest_tokens is not None:
        return ParsedProviderUsage(status="parsed", tokens=latest_tokens)
    if malformed_error is not None:
        return ParsedProviderUsage(status="malformed", error=malformed_error)
    if not saw_usage_candidate:
        return ParsedProviderUsage(status="none")
    return ParsedProviderUsage(status="none")


def load_structured_output_payload(
    stdout_text: str,
    stderr_text: str,
) -> tuple[JsonObject | None, str | None]:
    return load_structured_output_payload_from_lines(
        stdout_text.splitlines(),
        stderr_text.splitlines(),
    )


def load_structured_output_payload_from_lines(
    stdout_lines: Iterable[str],
    stderr_lines: Iterable[str],
) -> tuple[JsonObject | None, str | None]:
    for text in (stdout_lines, stderr_lines):
        for line in text:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload: JsonValue = json.loads(stripped)
            except json.JSONDecodeError as exc:
                return (
                    None,
                    f"Malformed structured output: {exc.msg}",
                )
            if not isinstance(payload, dict):
                return (
                    None,
                    "Malformed structured output: expected a JSON object.",
                )
            return payload, None
    return None, None


def find_usage_mapping(payload: JsonValue) -> JsonObject | None:
    if isinstance(payload, dict):
        usage = payload.get("usage")
        if isinstance(usage, dict):
            return cast(JsonObject, usage)
        response = payload.get("response")
        if isinstance(response, dict):
            nested = response.get("usage")
            if isinstance(nested, dict):
                return cast(JsonObject, nested)
        for value in payload.values():
            candidate = find_usage_mapping(value)
            if candidate is not None:
                return candidate
    elif isinstance(payload, list):
        for item in payload:
            candidate = find_usage_mapping(item)
            if candidate is not None:
                return candidate
    return None


def parse_usage_mapping(payload: object) -> ParsedProviderUsage:
    if not isinstance(payload, dict):
        return ParsedProviderUsage(
            status="malformed",
            error="Malformed provider usage payload: expected an object.",
        )

    values: dict[str, int | None] = {}
    for bucket in TOKEN_BUCKETS:
        token_value, error = extract_bucket_value(payload, bucket)
        if error is not None:
            return ParsedProviderUsage(status="malformed", error=error)
        values[bucket] = token_value

    tokens = ProviderTokenUsage(
        input=values["input"],
        cached_input=values["cached_input"],
        cache_write=values["cache_write"],
        output=values["output"],
        reasoning=values["reasoning"],
        total=values["total"],
    )
    if not tokens.has_any_value():
        return ParsedProviderUsage(status="none")
    return ParsedProviderUsage(status="parsed", tokens=tokens)


def extract_bucket_value(
    payload: Mapping[str, object],
    bucket: TokenBucket,
) -> tuple[int | None, str | None]:
    for key in TOKEN_KEY_ALIASES[bucket]:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool):
            return None, invalid_usage_value(bucket, value)
        if isinstance(value, int):
            if value < 0:
                return None, invalid_usage_value(bucket, value)
            return value, None
        if isinstance(value, float) and value.is_integer():
            if value < 0:
                return None, invalid_usage_value(bucket, value)
            return int(value), None
        return None, invalid_usage_value(bucket, value)
    return None, None


def invalid_usage_value(bucket: TokenBucket, value: object) -> str:
    return (
        "Malformed provider usage payload: "
        f"{bucket} must be a non-negative integer, got {value!r}."
    )
