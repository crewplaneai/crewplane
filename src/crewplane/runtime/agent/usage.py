from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts import (
    CommandResult,
    OutputExtractionStatus,
    ProviderKind,
)
from crewplane.core.config import AgentConfig

from .usage_costs import (
    classify_provider_usage_status,
    derive_configured_cost,
    estimate_token_count,
    roll_up_cost_confidence,
)
from .usage_parsing import (
    parse_provider_usage,
    parse_provider_usage_from_result,
)
from .usage_types import (
    VISIBLE_ESTIMATE_METHOD,
    InvocationUsage,
    ParsedProviderUsage,
    ProviderTokenUsage,
    UsageParser,
)

__all__ = [
    "InvocationUsage",
    "InvocationUsageAccumulator",
    "ParsedProviderUsage",
    "ProviderTokenUsage",
    "UsageParser",
    "build_fallback_usage",
    "build_fallback_usage_from_output_file",
    "classify_provider_usage_status",
    "estimate_token_count",
    "parse_provider_usage_from_result",
    "output_text_for_usage",
    "parse_provider_usage",
    "provider_kind_for_config",
    "roll_up_cost_confidence",
]

MAX_USAGE_OUTPUT_BYTES = 1_048_576


def build_fallback_usage(
    prompt: str,
    output_text: str,
    config: AgentConfig,
    attempt_count: int = 1,
) -> InvocationUsage:
    return build_fallback_usage_from_output_chars(
        prompt=prompt,
        output_char_count=len(output_text),
        config=config,
        attempt_count=attempt_count,
    )


def build_fallback_usage_from_output_file(
    prompt: str,
    output_file: Path,
    config: AgentConfig,
    attempt_count: int = 1,
) -> InvocationUsage:
    output_char_count = 0
    if output_file.exists():
        output_char_count = _decoded_character_count(output_file)
    return build_fallback_usage_from_output_chars(
        prompt=prompt,
        output_char_count=output_char_count,
        config=config,
        attempt_count=attempt_count,
    )


def build_fallback_usage_from_output_chars(
    prompt: str,
    output_char_count: int,
    config: AgentConfig,
    attempt_count: int = 1,
) -> InvocationUsage:
    visible_input_tokens = estimate_token_count(len(prompt) * attempt_count)
    visible_output_tokens = estimate_token_count(output_char_count)
    visible_estimate_tokens = visible_input_tokens + visible_output_tokens
    configured_cost_usd, confidence = derive_configured_cost(
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


def output_text_for_usage(result: CommandResult) -> str:
    if result.returncode == 0:
        stdout_text = _bounded_result_stream_text(
            fallback_text=result.stdout_text,
            path=result.stdout_path,
        )
        if stdout_text.strip():
            return stdout_text
        stderr_text = _bounded_result_stream_text(
            fallback_text=result.stderr_text,
            path=result.stderr_path,
        )
        if stderr_text.strip():
            return stderr_text
        return ""
    stderr_text = _bounded_result_stream_text(
        fallback_text=result.stderr_text,
        path=result.stderr_path,
    )
    stdout_text = _bounded_result_stream_text(
        fallback_text=result.stdout_text,
        path=result.stdout_path,
    )
    if stderr_text:
        return f"{stderr_text}{stdout_text}"
    return stdout_text


def provider_kind_for_config(config: AgentConfig) -> ProviderKind:
    return config.provider_kind


class InvocationUsageAccumulator:
    def __init__(self, provider_kind: ProviderKind, prompt: str) -> None:
        self._provider_kind = provider_kind
        self._prompt_chars_per_attempt = len(prompt)
        self._attempt_count = 0
        self._visible_output_chars_total = 0

    @property
    def provider_kind(self) -> ProviderKind:
        return self._provider_kind

    def record_attempt_start(self) -> None:
        self._attempt_count += 1

    def record_attempt_output(self, output_text: str) -> None:
        self.record_attempt_output_chars(len(output_text))

    def record_attempt_output_chars(self, char_count: int) -> None:
        self._visible_output_chars_total += max(0, char_count)

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
        configured_cost_usd, confidence = derive_configured_cost(
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


def _bounded_result_stream_text(fallback_text: str, path: Path | None) -> str:
    if path is not None and path.is_file():
        with path.open("rb") as handle:
            return handle.read(MAX_USAGE_OUTPUT_BYTES).decode(
                "utf-8",
                errors="replace",
            )
    return _bounded_text(fallback_text)


def _bounded_text(value: str) -> str:
    return value.encode("utf-8")[:MAX_USAGE_OUTPUT_BYTES].decode(
        "utf-8",
        errors="replace",
    )


def _decoded_character_count(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while chunk := handle.read(MAX_USAGE_OUTPUT_BYTES):
            count += len(chunk)
    return count
