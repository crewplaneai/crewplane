from dataclasses import replace

from crewplane.adapters.invokers.cli_invoker import (
    CliCommand,
    CliInvocationRequest,
    CliProviderCapability,
    extract_strict_stdout,
)


def validate_request(request: CliInvocationRequest) -> None:
    if request.requested_reasoning is not None:
        raise ValueError("fixture does not support reasoning")


def build_command(request: CliInvocationRequest, prompt: str) -> CliCommand:
    return CliCommand(
        [*request.config.cli_cmd, *request.config.extra_args], prompt.encode("utf-8")
    )


FIXTURE = CliProviderCapability(
    provider_kind="fixture",
    validate_request=validate_request,
    build_command=build_command,
    output_extractor=extract_strict_stdout,
)
ALTERNATE = replace(FIXTURE, log_presentation_profile="alternate")
WRONG_IDENTITY = replace(FIXTURE, provider_kind="another")
INVALID_CALLBACK = replace(FIXTURE, build_command=None)  # type: ignore[arg-type]
INVALID_EXPORT = object()
