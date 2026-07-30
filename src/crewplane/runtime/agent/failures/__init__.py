from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts import CommandResult, ProviderKind

from .classifier import classify_invocation_failure
from .patterns import ADVICE_BY_KIND
from .types import (
    FailureEvidence,
    FailureKind,
    FailurePhase,
    FailureSource,
    InvocationFailureError,
    InvocationFailureSummary,
)

__all__ = [
    "FailureEvidence",
    "FailureKind",
    "FailurePhase",
    "FailureSource",
    "InvocationFailureError",
    "InvocationFailureSummary",
    "build_adapter_invocation_failure_error",
    "build_invocation_failure_error",
    "build_output_extraction_failure_error",
    "build_quota_failure_error",
    "classify_invocation_failure",
]


def build_adapter_invocation_failure_error(
    error: RuntimeError,
    log_file: Path | None,
) -> InvocationFailureError:
    summary = InvocationFailureSummary(
        kind="provider_error",
        phase="provider_transport",
        source="none",
        message=str(error),
        advice="The configured invoker reported a provider failure.",
        condensed=False,
    )
    return InvocationFailureError("provider invocation failed", summary, log_file)


def build_invocation_failure_error(
    prefix: str,
    provider_kind: ProviderKind,
    result: CommandResult,
    log_file: Path | None,
) -> InvocationFailureError:
    return InvocationFailureError(
        prefix,
        classify_invocation_failure(provider_kind, result),
        log_file,
    )


def build_output_extraction_failure_error(
    cli_executable: str,
    extraction_status: str,
) -> InvocationFailureError:
    summary = InvocationFailureSummary(
        kind="malformed_provider_output",
        phase="provider_output",
        source="none",
        message=extraction_status,
        advice=ADVICE_BY_KIND["malformed_provider_output"],
        condensed=False,
    )
    return InvocationFailureError(
        f"{cli_executable} output extraction failed",
        summary,
        None,
    )


def build_quota_failure_error(
    prefix: str,
    provider_kind: ProviderKind,
    result: CommandResult,
    log_file: Path | None,
    last_non_quota_failure: InvocationFailureSummary | None = None,
) -> InvocationFailureError:
    summary = classify_invocation_failure(provider_kind, result)
    if summary.kind != "quota_or_rate_limit":
        summary = InvocationFailureSummary(
            kind="quota_or_rate_limit",
            phase="provider_transport",
            source=summary.source,
            message=summary.message,
            advice=ADVICE_BY_KIND["quota_or_rate_limit"],
            condensed=summary.condensed,
        )
    if last_non_quota_failure is not None:
        prefix = (
            f"{prefix}; last distinct non-quota failure: "
            f"{last_non_quota_failure.message}"
        )
    error = InvocationFailureError(prefix, summary, log_file)
    error.last_non_quota_failure = last_non_quota_failure
    if last_non_quota_failure is not None:
        error.add_note(
            "Last distinct non-quota failure before quota retries: "
            f"{last_non_quota_failure.message}"
        )
    return error
