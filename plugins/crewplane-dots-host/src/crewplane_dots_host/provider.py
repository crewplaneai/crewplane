"""Native receipt prose is never a provider quota signal."""

from datetime import datetime

from crewplane.adapters.invokers.cli_invoker import (
    CliCommand,
    CliInvocationRequest,
    CliProviderCapability,
    extract_strict_stdout,
)
from crewplane.architecture.contracts import CommandResult, QuotaClassification


def validate_request(request: CliInvocationRequest) -> None:
    config = request.config
    if config.prompt_transport != "stdin" or config.prompt_transport_arg is not None:
        raise ValueError("Dots host requires plain stdin prompt transport")
    if request.requested_reasoning is not None:
        raise ValueError("Use explicit dots-host --reasoning in cli_cmd/extra_args")
    if request.model is not None and (
        not request.model.strip() or request.model.startswith("-")
    ):
        raise ValueError(
            "Dots model must be a nonblank model identifier, not an option"
        )
    if any(
        arg == "--model" or arg.startswith("--model=")
        for arg in [*config.cli_cmd[1:], *config.extra_args]
    ):
        raise ValueError("Dots model is managed by the workflow/default_model")


def build_command(request: CliInvocationRequest, prompt: str) -> CliCommand:
    validate_request(request)
    command = request.config.get_command()
    command[0] = request.command_strategy.prepare_executable(command[0], request)
    if request.model is not None:
        command.extend(["--model", request.model])
    command.extend(request.config.extra_args)
    return CliCommand(command, prompt.encode("utf-8"))


def classify_quota(
    result: CommandResult, patterns: tuple[str, ...], now: datetime
) -> QuotaClassification:
    del result, patterns, now
    return QuotaClassification(False, None, None)


DOTS_HOST = CliProviderCapability(
    provider_kind="dots_host",
    validate_request=validate_request,
    build_command=build_command,
    output_extractor=extract_strict_stdout,
    quota_classifier=classify_quota,
    supports_output_idle_timeout=False,
)
