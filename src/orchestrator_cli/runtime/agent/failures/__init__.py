from __future__ import annotations

from pathlib import Path

from ..command_builder import ProviderKind
from ..types import CommandResult
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
    "build_invocation_failure_error",
    "build_output_extraction_failure_error",
    "build_quota_failure_error",
    "classify_invocation_failure",
]


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
    return InvocationFailureError(prefix, summary, log_file)
