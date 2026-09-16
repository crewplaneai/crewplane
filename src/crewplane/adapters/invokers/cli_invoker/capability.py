from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from crewplane.architecture.contracts import (
    FailureClassifier,
    LogPresentationFormat,
    OneShotFailureRetryPolicy,
    OutputExtractor,
    ProviderKind,
    QuotaClassifier,
    UsageDecoder,
)
from crewplane.core.config import AgentConfig

from .failures.classifier import classify_generic_failure
from .quota.classifier import classify_generic_quota


@dataclass(frozen=True)
class CliInvocationRequest:
    """Transient validation inputs shared by preflight and command planning."""

    config: AgentConfig
    model: str | None
    requested_reasoning: str | None = None
    working_directory: Path | None = None
    environment: Mapping[str, str] = field(default_factory=os.environ.copy)


@dataclass(frozen=True)
class CliCommand:
    """A returned command transfers ownership of its optional answer path."""

    cmd: list[str]
    stdin_data: bytes | None
    structured_output_file: Path | None = None


@dataclass(frozen=True)
class CliProviderCapability:
    """Adapter-owned strategies for one CLI family; callbacks borrow streams."""

    provider_kind: ProviderKind
    validate_request: Callable[[CliInvocationRequest], None]
    build_command: Callable[[CliInvocationRequest, str], CliCommand]
    output_extractor: OutputExtractor | None
    usage_decoder: UsageDecoder | None = None
    quota_classifier: QuotaClassifier = classify_generic_quota
    failure_classifier: FailureClassifier = classify_generic_failure
    log_presentation_format: LogPresentationFormat = "plain"
    log_presentation_profile: str = "generic"
    one_shot_failure_retry: OneShotFailureRetryPolicy | None = None
    supports_output_idle_timeout: bool = True
