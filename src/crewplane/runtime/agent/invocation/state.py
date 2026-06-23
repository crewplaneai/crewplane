from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from crewplane.architecture.contracts import (
    CommandResult,
    FailureClassificationProfile,
    InvocationLogLevel,
    OutputExtractionMode,
    QuotaParserProfile,
    RuntimeLogValue,
    StructuredOutputMode,
    UsageParserProfile,
)

from ..usage import (
    InvocationUsageAccumulator,
    OutputExtractionStatus,
    ParsedProviderUsage,
)


@dataclass(frozen=True)
class InvocationDiagnosticNotice:
    level: InvocationLogLevel
    message: str
    operation: str
    attributes: dict[str, RuntimeLogValue] | None = None
    console_message: str | None = None


@dataclass(frozen=True)
class ExtractedInvocationOutput:
    output_text: str
    output_extraction_status: OutputExtractionStatus
    parsed_provider_usage: ParsedProviderUsage
    notice: InvocationDiagnosticNotice | None = None
    output_path: Path | None = None
    output_char_count: int | None = None
    owns_output_path: bool = False


@dataclass(frozen=True)
class InvocationCommandRuntime:
    failure_profile: FailureClassificationProfile
    structured_output_mode: StructuredOutputMode
    output_extraction_mode: OutputExtractionMode
    quota_parser: QuotaParserProfile
    usage_parser: UsageParserProfile
    structured_output_file: Path | None
    cmd: list[str]
    stdin_data: bytes | None
    log_header: bytes


@dataclass
class InvocationUsageState:
    accumulator: InvocationUsageAccumulator
    output_extraction_status: OutputExtractionStatus = "missing"
    parsed_provider_usage: ParsedProviderUsage = field(
        default_factory=lambda: ParsedProviderUsage(status="none")
    )
    usage_recorded: bool = False

    def record_attempt_output(self, output_text: str) -> None:
        self.accumulator.record_attempt_output(output_text)

    def record_attempt_output_chars(self, char_count: int) -> None:
        self.accumulator.record_attempt_output_chars(char_count)

    def record_extracted_output(
        self,
        extracted_output: ExtractedInvocationOutput,
    ) -> None:
        self.output_extraction_status = extracted_output.output_extraction_status
        self.parsed_provider_usage = extracted_output.parsed_provider_usage
        if extracted_output.output_char_count is not None:
            self.record_attempt_output_chars(extracted_output.output_char_count)
            return
        self.record_attempt_output(extracted_output.output_text)


@dataclass(frozen=True)
class InvocationAttemptResult:
    result: CommandResult
    extracted_output: ExtractedInvocationOutput | None
    usage_output: str


@dataclass(frozen=True)
class InvocationRetryCursor:
    retry_count: int
    quota_retry_count: int
    quota_retry_started_at: float | None


@dataclass(frozen=True)
class AttemptTransitionState:
    retry_count: int
    quota_retry_count: int
    quota_retry_started_at: float | None

    def cursor(self) -> InvocationRetryCursor:
        return InvocationRetryCursor(
            retry_count=self.retry_count,
            quota_retry_count=self.quota_retry_count,
            quota_retry_started_at=self.quota_retry_started_at,
        )


@dataclass(frozen=True)
class ContinueAttemptTransition(AttemptTransitionState):
    attempt_output_for_usage: str | None = None


@dataclass(frozen=True)
class SleepAndRetryAttemptTransition(AttemptTransitionState):
    retry_delay_seconds: float
    extracted_output: ExtractedInvocationOutput | None = None
    attempt_output_for_usage: str | None = None
    notice: InvocationDiagnosticNotice | None = None


@dataclass(frozen=True)
class FinalizeSuccessAttemptTransition(AttemptTransitionState):
    extracted_output: ExtractedInvocationOutput


@dataclass(frozen=True)
class RaiseRetryExhaustedAttemptTransition(AttemptTransitionState):
    extracted_output: ExtractedInvocationOutput | None = None
    attempt_output_for_usage: str | None = None


@dataclass(frozen=True)
class RaiseFailedExitAttemptTransition(AttemptTransitionState):
    attempt_output_for_usage: str | None = None


@dataclass(frozen=True)
class RaiseQuotaFailureAttemptTransition(AttemptTransitionState):
    message: str
    attempt_output_for_usage: str | None = None


@dataclass(frozen=True)
class RaiseOutputExtractionFailureAttemptTransition(AttemptTransitionState):
    extracted_output: ExtractedInvocationOutput
    attempt_output_for_usage: str | None = None


type InvocationAttemptTransition = (
    ContinueAttemptTransition
    | SleepAndRetryAttemptTransition
    | FinalizeSuccessAttemptTransition
    | RaiseRetryExhaustedAttemptTransition
    | RaiseFailedExitAttemptTransition
    | RaiseQuotaFailureAttemptTransition
    | RaiseOutputExtractionFailureAttemptTransition
)
