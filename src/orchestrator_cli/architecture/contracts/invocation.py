from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol, cast

from .json import JsonObject

# Config imports these contracts, so keep AgentConfig type-only to avoid a cycle.
if TYPE_CHECKING:
    from orchestrator_cli.core.config import AgentConfig

ProviderKind = Literal["claude", "codex", "copilot", "gemini", "kilo", "generic"]
PromptTransport = Literal["stdin", "argv"]
StructuredOutputMode = Literal["none", "codex_last_message_file", "claude_json"]
OutputExtractionMode = Literal["visible", "codex_last_message_file", "claude_json"]
OutputExtractionStatus = Literal["success", "missing", "malformed"]
QuotaParserProfile = Literal["codex", "copilot", "claude", "kilo", "gemini", "generic"]
UsageParserProfile = Literal["none", "codex", "claude"]
FailureClassificationProfile = ProviderKind
ProviderUsageStatus = Literal["full", "partial", "none", "malformed"]
InvocationCostConfidence = Literal["full", "partial", "none"]
AggregateCostConfidence = Literal["full", "partial", "none", "mixed"]
LogPresentationFormat = Literal["plain", "json_lines", "json_object"]

_LOG_PRESENTATION_FORMATS: frozenset[str] = frozenset(
    {"plain", "json_lines", "json_object"}
)
_LOG_PRESENTATION_PROFILE_PATTERN = re.compile(r"^[a-z0-9_.-]+$")
_MAX_LOG_PRESENTATION_PROFILE_LENGTH = 64


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

    def as_event_fields(self) -> JsonObject:
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


@dataclass(frozen=True)
class LogPresentationDescriptor:
    format: LogPresentationFormat
    profile: str = "generic"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "format",
            validate_log_presentation_format(self.format),
        )
        object.__setattr__(
            self,
            "profile",
            normalize_log_presentation_profile(self.profile),
        )


def validate_log_presentation_format(value: object) -> LogPresentationFormat:
    if not isinstance(value, str):
        raise TypeError("log presentation format must be a string")
    if value not in _LOG_PRESENTATION_FORMATS:
        raise ValueError(f"unsupported log presentation format: {value!r}")
    return cast(LogPresentationFormat, value)


def normalize_log_presentation_profile(profile: str) -> str:
    if not isinstance(profile, str):
        raise TypeError("log presentation profile must be a string")
    normalized = profile.strip().lower()
    if not normalized:
        raise ValueError("log presentation profile must not be empty")
    if len(normalized) > _MAX_LOG_PRESENTATION_PROFILE_LENGTH:
        raise ValueError("log presentation profile is too long")
    if _LOG_PRESENTATION_PROFILE_PATTERN.fullmatch(normalized) is None:
        raise ValueError("log presentation profile contains unsafe characters")
    return normalized


def validate_log_presentation_descriptor(
    value: object,
) -> LogPresentationDescriptor:
    if isinstance(value, LogPresentationDescriptor):
        return LogPresentationDescriptor(
            format=value.format,
            profile=value.profile,
        )
    if not isinstance(value, Mapping):
        raise TypeError("log presentation descriptor must be a descriptor or mapping")

    if "format" not in value:
        raise ValueError("log presentation descriptor missing format")
    profile = value.get("profile", "generic")
    return LogPresentationDescriptor(
        format=validate_log_presentation_format(value["format"]),
        profile=normalize_log_presentation_profile(profile),
    )


class AgentInvoker(Protocol):
    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        """Invoke an agent and write the output to a file."""

    def log_presentation_for(
        self,
        config: AgentConfig,
    ) -> LogPresentationDescriptor | None:
        """Return display-only log presentation metadata for an invocation."""


RuntimeLogValue = str | int | float | bool | None
InvocationLogLevel = Literal["debug", "info", "warning", "error"]


@dataclass(frozen=True)
class InvocationDiagnostic:
    level: InvocationLogLevel
    message: str
    operation: str
    attributes: Mapping[str, RuntimeLogValue] | None = None

    def __post_init__(self) -> None:
        if self.attributes is not None:
            object.__setattr__(
                self,
                "attributes",
                MappingProxyType(dict(self.attributes)),
            )


InvocationDiagnosticSink = Callable[[InvocationDiagnostic], None]
InvocationUsageSink = Callable[[InvocationUsage], None]
ConsoleMessageSink = Callable[[str], None]


@dataclass(frozen=True)
class InvocationContext:
    node_id: str
    task_id: str
    provider: str
    role: str
    audit_round_num: int | None = None
    round_num: int | None = None
    findings_enabled: bool = False
    diagnostics: InvocationDiagnosticSink | None = None
    usage_recorder: InvocationUsageSink | None = None
    console_message_sink: ConsoleMessageSink | None = None


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout_text: str
    stderr_text: str
    stdout_path: Path | None = None
    stderr_path: Path | None = None

    @property
    def combined_output(self) -> str:
        return f"{self.stderr_text}\n{self.stdout_text}"

    def iter_stdout_lines(self) -> Iterator[str]:
        if self.stdout_path is not None and self.stdout_path.is_file():
            return _iter_lines_from_file(self.stdout_path)
        return iter(_iter_lines_from_text(self.stdout_text))

    def iter_stderr_lines(self) -> Iterator[str]:
        if self.stderr_path is not None and self.stderr_path.is_file():
            return _iter_lines_from_file(self.stderr_path)
        return iter(_iter_lines_from_text(self.stderr_text))

    def iter_combined_lines(self) -> Iterator[str]:
        yield from self.iter_stderr_lines()
        yield from self.iter_stdout_lines()

    def cleanup_stream_files(self) -> None:
        """Remove persisted stream capture files for this invocation result."""
        if self.stdout_path is not None:
            with suppress(OSError):
                self.stdout_path.unlink(missing_ok=True)
        if self.stderr_path is not None:
            with suppress(OSError):
                self.stderr_path.unlink(missing_ok=True)


def _iter_lines_from_file(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            line = handle.readline()
            if not line:
                return
            yield line.rstrip("\n")


def _iter_lines_from_text(value: str) -> list[str]:
    return [line for line in value.splitlines() if line]


@dataclass(frozen=True)
class QuotaClassification:
    is_quota: bool
    reset_after_seconds: float | None
    evidence: str | None


@dataclass(frozen=True)
class InvocationPlan:
    cmd: list[str]
    stdin_data: bytes | None
    structured_output_file: Path | None
    structured_output_mode: StructuredOutputMode
    output_extraction_mode: OutputExtractionMode
    quota_parser: QuotaParserProfile
    usage_parser: UsageParserProfile
    failure_profile: FailureClassificationProfile
    log_header: bytes
    log_provider_kind: ProviderKind


class CommandRunner(Protocol):
    async def __call__(
        self,
        cmd: list[str],
        stdin_data: bytes | None,
        log_file: Path | None,
        append_log: bool,
        log_header: bytes | None,
        invocation_context: InvocationContext | None,
        idle_timeout_seconds: float | None,
    ) -> CommandResult: ...
