"""Local Ollama text generation through the supported Crewplane CLI contract."""

from datetime import datetime

from crewplane.adapters.invokers.cli_invoker import (
    CliProviderCapability,
    CliProviderOnboarding,
    extract_strict_stdout,
)
from crewplane.architecture.contracts import CommandResult, QuotaClassification
from crewplane.architecture.contracts.invocation_failures import (
    InvocationFailureSummary,
)

from .availability import check_availability
from .command import build_command, validate_request
from .discovery import discover, list_models

__all__ = ["OLLAMA"]


def classify_quota(
    result: CommandResult, patterns: tuple[str, ...], now: datetime
) -> QuotaClassification:
    del result, patterns, now
    return QuotaClassification(False, None, None)


def classify_failure(result: CommandResult) -> InvocationFailureSummary:
    return InvocationFailureSummary(
        kind="provider_error",
        phase="unknown",
        source="stderr_text" if result.stderr_text else "stdout_text",
        message=(
            result.stderr_text
            or result.stdout_text
            or f"Ollama exited with status {result.returncode}"
        )[-2000:],
        advice="Check the local Ollama server, model, and invocation log.",
        condensed=False,
    )


OLLAMA = CliProviderCapability(
    provider_kind="ollama",
    validate_request=validate_request,
    build_command=build_command,
    output_extractor=extract_strict_stdout,
    usage_decoder=None,
    quota_classifier=classify_quota,
    failure_classifier=classify_failure,
    log_presentation_format="plain",
    check_availability=check_availability,
    onboarding=CliProviderOnboarding(discover=discover, list_models=list_models),
)
