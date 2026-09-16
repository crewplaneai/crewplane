from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from crewplane.architecture.contracts import (
    InvocationContext,
    InvocationPlan,
    LogPresentationDescriptor,
    ProviderKind,
)
from crewplane.architecture.contracts.provider_log import build_provider_log_header
from crewplane.core.config import AgentConfig

from .capability import CliInvocationRequest, CliProviderCapability
from .providers import claude, codex, copilot, deepseek, gemini, generic, kilo, pi

CAPABILITIES: dict[ProviderKind, CliProviderCapability] = {
    ProviderKind.CLAUDE: claude.CLAUDE,
    ProviderKind.CODEX: codex.CODEX,
    ProviderKind.COPILOT: copilot.COPILOT,
    ProviderKind.GEMINI: gemini.GEMINI,
    ProviderKind.KILO: kilo.KILO,
    ProviderKind.PI: pi.PI,
    ProviderKind.DEEPSEEK: deepseek.DEEPSEEK,
    ProviderKind.GENERIC: generic.GENERIC,
}


def get_cli_provider_capability(provider_kind: ProviderKind) -> CliProviderCapability:
    """Fail explicitly when a schema family has no installed implementation."""
    try:
        return CAPABILITIES[ProviderKind(provider_kind)]
    except KeyError as exc:
        raise ValueError(
            f"No CLI capability registered for {provider_kind!r}."
        ) from exc


def build_cli_invocation_plan(
    config: AgentConfig,
    model: str | None,
    prompt: str,
    output_file: Path,
    invocation_context: InvocationContext | None = None,
    working_directory: Path | None = None,
) -> InvocationPlan:
    """Validate before allocation, then transfer ownership of a complete plan."""
    capability = get_cli_provider_capability(config.provider_kind)
    request = CliInvocationRequest(
        config=config,
        model=model,
        requested_reasoning=(
            invocation_context.requested_reasoning
            if invocation_context is not None
            else None
        ),
        working_directory=working_directory,
    )
    capability.validate_request(request)
    command = capability.build_command(request, prompt)
    try:
        return InvocationPlan(
            cmd=command.cmd,
            stdin_data=command.stdin_data,
            structured_output_file=command.structured_output_file,
            output_extractor=capability.output_extractor,
            usage_decoder=capability.usage_decoder,
            quota_classifier=capability.quota_classifier,
            failure_classifier=capability.failure_classifier,
            log_header=build_provider_log_header(
                started_at=datetime.now(UTC).isoformat(),
                cli_executable=command.cmd[0],
                model=model,
                output_file=output_file,
                requested_reasoning=request.requested_reasoning,
            ),
            one_shot_failure_retry=capability.one_shot_failure_retry,
            supports_output_idle_timeout=capability.supports_output_idle_timeout,
        )
    except BaseException:
        if command.structured_output_file is not None:
            command.structured_output_file.unlink(missing_ok=True)
        raise


def build_cli_log_presentation(config: AgentConfig) -> LogPresentationDescriptor:
    """Return display-only log metadata declared by the provider capability."""
    capability = get_cli_provider_capability(config.provider_kind)
    return LogPresentationDescriptor(
        format=capability.log_presentation_format,
        profile=capability.log_presentation_profile,
    )
