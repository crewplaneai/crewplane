from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from math import ceil
from types import MappingProxyType
from typing import Literal, cast

from orchestrator_cli.core.config import AgentConfig

from .command_builder import ProviderKind, provider_kind
from .types import CommandResult

OutputExtractionStatus = Literal["success", "missing", "malformed"]
ProviderUsageStatus = Literal["full", "partial", "none", "malformed"]
InvocationCostConfidence = Literal["full", "partial", "none"]
AggregateCostConfidence = Literal["full", "partial", "none", "mixed"]
VisibleEstimateMethod = Literal["char-count-lower-bound"]
UsageParser = Literal["none", "codex", "claude"]
TokenBucket = Literal[
    "input",
    "cached_input",
    "cache_write",
    "output",
    "reasoning",
    "total",
]
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

TOKEN_BUCKETS: tuple[TokenBucket, ...] = (
    "input",
    "cached_input",
    "cache_write",
    "output",
    "reasoning",
    "total",
)
TOKENS_PER_MILLION = 1_000_000
VISIBLE_ESTIMATE_METHOD: VisibleEstimateMethod = "char-count-lower-bound"
VISIBLE_OUTPUT_PROVIDER_KINDS: frozenset[ProviderKind] = frozenset(
    {"generic", "copilot", "gemini", "kilo"}
)
STRUCTURED_PROVIDER_KINDS: frozenset[ProviderKind] = frozenset({"claude", "codex"})
AUTO_USAGE_PARSER_BY_EXECUTABLE: dict[str, UsageParser] = {
    "claude": "claude",
    "codex": "codex",
}
TOKEN_KEY_ALIASES: dict[TokenBucket, tuple[str, ...]] = {
    "input": ("input_tokens", "prompt_tokens", "input"),
    "cached_input": ("cached_input_tokens", "cache_read_input_tokens"),
    "cache_write": ("cache_write_tokens",),
    "output": ("output_tokens", "completion_tokens", "output"),
    "reasoning": ("reasoning_tokens",),
    "total": ("total_tokens", "total"),
}


@dataclass(frozen=True)
class ProviderTokenUsage:
    input: int | None = None
    cached_input: int | None = None
    cache_write: int | None = None
    output: int | None = None
    reasoning: int | None = None
    total: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "input": self.input,
            "cached_input": self.cached_input,
            "cache_write": self.cache_write,
            "output": self.output,
            "reasoning": self.reasoning,
            "total": self.total,
        }

    def has_any_value(self) -> bool:
        return any(value is not None for value in self.as_dict().values())


@dataclass(frozen=True)
class ParsedProviderUsage:
    status: Literal["parsed", "none", "malformed"]
    tokens: ProviderTokenUsage | None = None
    error: str | None = None


@dataclass(frozen=True)
class InvocationUsage:
    attempt_count: int
    cli_captured: bool
    output_extraction_status: OutputExtractionStatus
    provider_usage_status: ProviderUsageStatus
    provider_tokens: Mapping[str, int | None]
    visible_estimate_tokens: int | None
    visible_estimate_method: str | None
    visible_estimate_is_lower_bound: bool
    configured_cost_usd: float | None
    invocation_cost_confidence: InvocationCostConfidence
    usage_parse_error: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_tokens",
            MappingProxyType(dict(self.provider_tokens)),
        )

    def as_event_fields(self) -> dict[str, object]:
        return {
            "attempt_count": self.attempt_count,
            "cli_captured": self.cli_captured,
            "output_extraction_status": self.output_extraction_status,
            "provider_usage_status": self.provider_usage_status,
            "provider_tokens": dict(self.provider_tokens),
            "visible_estimate_tokens": self.visible_estimate_tokens,
            "visible_estimate_method": self.visible_estimate_method,
            "visible_estimate_is_lower_bound": self.visible_estimate_is_lower_bound,
            "configured_cost_usd": self.configured_cost_usd,
            "invocation_cost_confidence": self.invocation_cost_confidence,
            "usage_parse_error": self.usage_parse_error,
        }


def build_fallback_usage(
    prompt: str,
    output_text: str,
    config: AgentConfig,
    attempt_count: int = 1,
) -> InvocationUsage:
    visible_input_tokens = estimate_token_count(len(prompt) * attempt_count)
    visible_output_tokens = estimate_token_count(len(output_text))
    visible_estimate_tokens = visible_input_tokens + visible_output_tokens
    configured_cost_usd, confidence = _derive_configured_cost(
        config=config,
        provider_tokens=ProviderTokenUsage(),
        visible_input_tokens=visible_input_tokens,
        visible_output_tokens=visible_output_tokens,
        visible_estimate_tokens=visible_estimate_tokens,
    )
    return InvocationUsage(
        attempt_count=attempt_count,
        cli_captured=True,
        output_extraction_status="success",
        provider_usage_status="none",
        provider_tokens=ProviderTokenUsage().as_dict(),
        visible_estimate_tokens=visible_estimate_tokens,
        visible_estimate_method=VISIBLE_ESTIMATE_METHOD,
        visible_estimate_is_lower_bound=True,
        configured_cost_usd=configured_cost_usd,
        invocation_cost_confidence=confidence,
        usage_parse_error=None,
    )


def estimate_token_count(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return ceil(char_count / 4)


def output_text_for_usage(result: CommandResult) -> str:
    if result.returncode == 0:
        if result.stdout_text.strip():
            return result.stdout_text
        if result.stderr_text.strip():
            return result.stderr_text
        return ""
    return f"{result.stderr_text}{result.stdout_text}"


def parse_provider_usage(
    provider_kind: ProviderKind,
    stdout_text: str,
    stderr_text: str,
) -> ParsedProviderUsage:
    parser = _resolve_usage_parser(provider_kind)
    if parser == "codex":
        return _parse_codex_usage(stdout_text, stderr_text)
    if parser == "claude":
        payload, error = _load_structured_output_payload(stdout_text, stderr_text)
        if error is not None:
            return ParsedProviderUsage(status="malformed", error=error)
        if payload is None:
            return ParsedProviderUsage(status="none")
        usage_payload = payload.get("usage")
        if usage_payload is None:
            return ParsedProviderUsage(status="none")
        return _parse_usage_mapping(usage_payload)
    return ParsedProviderUsage(status="none")


def classify_provider_usage_status(
    config: AgentConfig,
    provider_kind: ProviderKind,
    parsed_usage: ParsedProviderUsage,
) -> ProviderUsageStatus:
    if provider_kind not in STRUCTURED_PROVIDER_KINDS:
        return "none"
    if parsed_usage.status == "malformed":
        return "malformed"
    if parsed_usage.status != "parsed" or parsed_usage.tokens is None:
        return "none"

    required_buckets = _provider_usage_buckets(config)
    known_tokens = parsed_usage.tokens.as_dict()
    known_required_buckets = [
        bucket for bucket in required_buckets if known_tokens[bucket] is not None
    ]
    if not known_required_buckets:
        return "none"
    if len(known_required_buckets) == len(required_buckets):
        return "full"
    return "partial"


def roll_up_cost_confidence(
    invocation_usages: tuple[InvocationUsage, ...],
) -> AggregateCostConfidence:
    if not invocation_usages:
        return "none"
    confidences = {usage.invocation_cost_confidence for usage in invocation_usages}
    if confidences == {"full"}:
        return "full"
    if confidences == {"none"}:
        return "none"
    if confidences <= {"full", "partial"} and "partial" in confidences:
        return "partial"
    return "mixed"


def _resolve_usage_parser(provider_kind: ProviderKind) -> UsageParser:
    if provider_kind not in STRUCTURED_PROVIDER_KINDS:
        return "none"
    return AUTO_USAGE_PARSER_BY_EXECUTABLE.get(provider_kind, "none")


def provider_kind_for_config(config: AgentConfig) -> ProviderKind:
    return provider_kind(config.cli_cmd[0])


class InvocationUsageAccumulator:
    def __init__(self, cli_executable: str, prompt: str) -> None:
        self._provider_kind = provider_kind(cli_executable)
        self._prompt_chars_per_attempt = len(prompt)
        self._attempt_count = 0
        self._visible_output_chars_total = 0

    @property
    def provider_kind(self) -> ProviderKind:
        return self._provider_kind

    def record_attempt_start(self) -> None:
        self._attempt_count += 1

    def record_attempt_output(self, output_text: str) -> None:
        self._visible_output_chars_total += len(output_text)

    def build_usage(
        self,
        config: AgentConfig,
        output_extraction_status: OutputExtractionStatus,
        parsed_usage: ParsedProviderUsage,
    ) -> InvocationUsage:
        visible_input_tokens = estimate_token_count(
            self._prompt_chars_per_attempt * self._attempt_count
        )
        visible_output_tokens = estimate_token_count(self._visible_output_chars_total)
        visible_estimate_tokens = visible_input_tokens + visible_output_tokens
        provider_tokens = parsed_usage.tokens or ProviderTokenUsage()
        configured_cost_usd, confidence = _derive_configured_cost(
            config=config,
            provider_tokens=provider_tokens,
            visible_input_tokens=visible_input_tokens,
            visible_output_tokens=visible_output_tokens,
            visible_estimate_tokens=visible_estimate_tokens,
        )
        return InvocationUsage(
            attempt_count=self._attempt_count,
            cli_captured=output_extraction_status == "success",
            output_extraction_status=output_extraction_status,
            provider_usage_status=classify_provider_usage_status(
                config=config,
                provider_kind=self._provider_kind,
                parsed_usage=parsed_usage,
            ),
            provider_tokens=provider_tokens.as_dict(),
            visible_estimate_tokens=visible_estimate_tokens,
            visible_estimate_method=VISIBLE_ESTIMATE_METHOD,
            visible_estimate_is_lower_bound=True,
            configured_cost_usd=configured_cost_usd,
            invocation_cost_confidence=confidence,
            usage_parse_error=parsed_usage.error,
        )


def _derive_configured_cost(
    config: AgentConfig,
    provider_tokens: ProviderTokenUsage,
    visible_input_tokens: int,
    visible_output_tokens: int,
    visible_estimate_tokens: int,
) -> tuple[float | None, InvocationCostConfidence]:
    pricing = config.pricing.as_dict()
    configured_buckets = [
        bucket for bucket, rate in pricing.items() if rate is not None
    ]
    if not configured_buckets:
        return None, "none"

    total_cost = 0.0
    computed_bucket_count = 0
    missing_bucket_count = 0
    fallback_bucket_count = 0
    provider_token_map = provider_tokens.as_dict()
    for bucket in configured_buckets:
        token_count = provider_token_map[bucket]
        if token_count is None:
            token_count = _visible_cost_token_count(
                bucket=bucket,
                visible_input_tokens=visible_input_tokens,
                visible_output_tokens=visible_output_tokens,
                visible_estimate_tokens=visible_estimate_tokens,
            )
            if token_count is not None:
                fallback_bucket_count += 1
        if token_count is None:
            missing_bucket_count += 1
            continue
        rate = pricing[bucket]
        if rate is None:
            continue
        total_cost += token_count * rate / TOKENS_PER_MILLION
        computed_bucket_count += 1

    if computed_bucket_count == 0:
        return None, "none"
    if missing_bucket_count == 0 and fallback_bucket_count == 0:
        return total_cost, "full"
    return total_cost, "partial"


def _visible_cost_token_count(
    bucket: str,
    visible_input_tokens: int,
    visible_output_tokens: int,
    visible_estimate_tokens: int,
) -> int | None:
    if bucket == "input":
        return visible_input_tokens
    if bucket == "output":
        return visible_output_tokens
    if bucket == "total":
        return visible_estimate_tokens
    return None


def _provider_usage_buckets(config: AgentConfig) -> tuple[TokenBucket, ...]:
    pricing_buckets = config.pricing.configured_buckets()
    if "total" in pricing_buckets:
        return ("total",)
    required_buckets: list[TokenBucket] = ["input", "output"]
    optional_buckets: tuple[TokenBucket, ...] = (
        "cached_input",
        "cache_write",
        "reasoning",
    )
    for bucket in optional_buckets:
        if bucket in pricing_buckets:
            required_buckets.append(bucket)
    return tuple(required_buckets)


def _parse_codex_usage(stdout_text: str, stderr_text: str) -> ParsedProviderUsage:
    latest_tokens: ProviderTokenUsage | None = None
    malformed_error: str | None = None
    saw_usage_candidate = False
    for text in (stdout_text, stderr_text):
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            candidate = _find_usage_mapping(payload)
            if candidate is None:
                continue
            saw_usage_candidate = True
            parsed_usage = _parse_usage_mapping(candidate)
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


def _load_structured_output_payload(
    stdout_text: str,
    stderr_text: str,
) -> tuple[JsonObject | None, str | None]:
    for text in (stdout_text, stderr_text):
        stripped = text.strip()
        if not stripped:
            continue
        try:
            payload: JsonValue = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return (
                None,
                f"Malformed structured output: {exc.msg}",
            )
        if not _is_json_object(payload):
            return (
                None,
                "Malformed structured output: expected a JSON object.",
            )
        return payload, None
    return None, None


def _is_json_object(value: JsonValue) -> bool:
    return isinstance(value, dict)


def _find_usage_mapping(payload: JsonValue) -> JsonObject | None:
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
            candidate = _find_usage_mapping(value)
            if candidate is not None:
                return candidate
    elif isinstance(payload, list):
        for item in payload:
            candidate = _find_usage_mapping(item)
            if candidate is not None:
                return candidate
    return None


def _parse_usage_mapping(payload: object) -> ParsedProviderUsage:
    if not isinstance(payload, dict):
        return ParsedProviderUsage(
            status="malformed",
            error="Malformed provider usage payload: expected an object.",
        )

    values: dict[str, int | None] = {}
    for bucket in TOKEN_BUCKETS:
        token_value, error = _extract_bucket_value(payload, bucket)
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


def _extract_bucket_value(
    payload: Mapping[str, object],
    bucket: TokenBucket,
) -> tuple[int | None, str | None]:
    for key in TOKEN_KEY_ALIASES[bucket]:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool):
            return None, _invalid_usage_value(bucket, value)
        if isinstance(value, int):
            if value < 0:
                return None, _invalid_usage_value(bucket, value)
            return value, None
        if isinstance(value, float) and value.is_integer():
            if value < 0:
                return None, _invalid_usage_value(bucket, value)
            return int(value), None
        return None, _invalid_usage_value(bucket, value)
    return None, None


def _invalid_usage_value(bucket: TokenBucket, value: object) -> str:
    return (
        "Malformed provider usage payload: "
        f"{bucket} must be a non-negative integer, got {value!r}."
    )
