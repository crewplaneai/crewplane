from __future__ import annotations

from functools import partial

from crewplane.architecture.contracts import (
    ProviderKind,
)

from ..capability import CliProviderCapability
from ..commands import build_standard_command
from ..failures.classifier import classify_generic_failure
from ..quota.classifier import classify_generic_quota
from ..validation import reject_unsupported_reasoning

QUOTA_HINTS = ("too many requests",)

COPILOT = CliProviderCapability(
    provider_kind=ProviderKind.COPILOT,
    validate_request=reject_unsupported_reasoning,
    build_command=build_standard_command,
    output_extractor=None,
    usage_decoder=None,
    quota_classifier=partial(classify_generic_quota, family_hints=QUOTA_HINTS),
    failure_classifier=classify_generic_failure,
    log_presentation_format="plain",
    log_presentation_profile="generic",
)
