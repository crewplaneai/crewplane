from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from crewplane.architecture.contracts import (
    FailureClassifier,
    LogPresentationFormat,
    OneShotFailureRetryPolicy,
    OutputExtractor,
    QuotaClassifier,
    UsageDecoder,
)
from crewplane.core.agent_environment import merge_agent_environment
from crewplane.core.config import AgentConfig, validate_provider_identifier

from . import command_strategy
from .discovery import CliProviderOnboarding
from .failures.classifier import classify_generic_failure
from .quota.classifier import classify_generic_quota


@dataclass(frozen=True)
class CliInvocationRequest:
    """Transient validation inputs shared by preflight and command planning."""

    config: AgentConfig
    model: str | None
    command_strategy: command_strategy.CommandStrategy = field(kw_only=True)
    requested_reasoning: str | None = None
    working_directory: Path | None = None
    environment: Mapping[str, str] = field(default_factory=os.environ.copy)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "environment",
            merge_agent_environment(self.environment, self.config.environment),
        )


@dataclass(frozen=True)
class CliCommand:
    """A returned command transfers ownership of its optional answer path."""

    cmd: list[str]
    stdin_data: bytes | None
    structured_output_file: Path | None = None


@dataclass(frozen=True)
class CliProviderCapability:
    """Strategies shared by overlapping requests; callbacks borrow streams.

    Builders own allocations until they return a command. Availability callbacks
    await their subprocess cleanup before returning or propagating cancellation,
    and own capture paths returned by their probe commands.
    """

    provider_kind: str
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
    check_availability: Callable[[CliInvocationRequest], Awaitable[None]] | None = None
    onboarding: CliProviderOnboarding | None = None

    def __post_init__(self) -> None:
        validate_provider_identifier(self.provider_kind)
