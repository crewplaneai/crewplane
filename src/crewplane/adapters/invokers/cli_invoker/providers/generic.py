from __future__ import annotations

from crewplane.architecture.contracts import (
    ProviderKind,
)

from ..capability import CliCommand, CliInvocationRequest, CliProviderCapability
from ..commands import build_standard_command
from ..failures.classifier import classify_generic_failure
from ..quota.classifier import classify_generic_quota
from ..validation import reject_unsupported_reasoning


def build_generic_command(request: CliInvocationRequest, prompt: str) -> CliCommand:
    return build_standard_command(request, prompt, model_arg=request.config.model_arg)


GENERIC = CliProviderCapability(
    provider_kind=ProviderKind.GENERIC,
    validate_request=reject_unsupported_reasoning,
    build_command=build_generic_command,
    output_extractor=None,
    usage_decoder=None,
    quota_classifier=classify_generic_quota,
    failure_classifier=classify_generic_failure,
    log_presentation_format="plain",
    log_presentation_profile="generic",
)
