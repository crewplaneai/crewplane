from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts import ProviderKind

from ..capability import CliCommand, CliInvocationRequest, CliProviderCapability
from ..commands import build_standard_command
from ..env_command import parse_env_command_context
from ..validation import reject_unsupported_reasoning
from .opencode_events import decode_opencode_usage, extract_opencode_output

_VALUE_OPTIONS = frozenset(
    {"--model", "-m", "--agent", "--variant", "--title", "--log-level"}
)
_BOOLEAN_OPTIONS = frozenset({"--thinking", "--auto", "--print-logs"})
_CONFLICTING_OPTIONS = frozenset(
    {
        "--format",
        "--continue",
        "-c",
        "--session",
        "-s",
        "--fork",
        "--attach",
        "--password",
        "-p",
        "--username",
        "-u",
        "--port",
        "--hostname",
        "--dir",
        "--file",
        "-f",
        "--command",
        "--interactive",
        "-i",
        "--mini",
        "--share",
        "--help",
        "-h",
        "--version",
        "-v",
        "--replay",
        "--replay-limit",
        "--no-replay",
        "--yolo",
        "--dangerously-skip-permissions",
        "--demo",
        "--",
    }
)


def validate_opencode_request(request: CliInvocationRequest) -> None:
    """Validate a fresh local run before command construction or allocation."""
    try:
        reject_unsupported_reasoning(request)
    except ValueError as exc:
        raise ValueError(f"OpenCode: {exc}") from exc
    config = request.config
    if config.prompt_transport != "stdin" or config.prompt_transport_arg is not None:
        raise ValueError(
            "OpenCode requires stdin transport without prompt_transport_arg."
        )
    arguments = _run_arguments(request)
    seen_options: set[str] = set()
    command_models = _validate_arguments(arguments, "cli_cmd", seen_options)
    extra_models = _validate_arguments(
        tuple(config.extra_args), "extra_args", seen_options
    )
    _validate_model_selection(command_models + extra_models, request.model)


def _run_arguments(request: CliInvocationRequest) -> tuple[str, ...]:
    try:
        context = parse_env_command_context(request.config.cli_cmd, None, "")
    except ValueError as exc:
        raise ValueError(f"OpenCode env wrapper validation failed: {exc}") from exc
    if context.command_working_directory_option is not None:
        raise ValueError(
            f"OpenCode option {context.command_working_directory_option!r} "
            "conflicts with the runtime-owned working directory."
        )
    if context.command_executable is None:
        raise ValueError("OpenCode requires an executable after the env wrapper.")
    arguments = context.command_arguments
    if not arguments or arguments[0] != "run":
        raise ValueError("OpenCode requires run immediately after the executable.")
    return arguments[1:]


def _validate_arguments(
    arguments: tuple[str, ...], location: str, seen_options: set[str]
) -> tuple[str, ...]:
    model_options: list[str] = []
    tokens = iter(enumerate(arguments, start=1))
    for position, token in tokens:
        option, separator, inline_value = token.partition("=")
        if option in _BOOLEAN_OPTIONS:
            _validate_boolean(option, inline_value if separator else None)
            continue
        if option not in _VALUE_OPTIONS:
            _reject_argument(option, location, position)
        value = inline_value if separator else next(tokens, (0, None))[1]
        _validate_value(option, value)
        if option in {"--model", "-m"}:
            model_options.append(option)
            continue
        if option in seen_options:
            raise ValueError(
                f"OpenCode option {option!r} may appear at most once "
                "across cli_cmd and extra_args."
            )
        seen_options.add(option)
    return tuple(model_options)


def _validate_boolean(option: str, value: str | None) -> None:
    if value is not None and value not in {"true", "false"}:
        raise ValueError(
            f"OpenCode option {option!r} requires a bare flag or =true/=false."
        )


def _validate_value(option: str, value: str | None) -> None:
    if value is None or not value.strip() or (value.startswith("-") and value != "-"):
        raise ValueError(f"OpenCode option {option!r} requires a nonblank value.")


def _reject_argument(option: str, location: str, position: int) -> None:
    if option in _CONFLICTING_OPTIONS:
        raise ValueError(
            f"OpenCode option {option!r} conflicts with managed fresh local runs."
        )
    raise ValueError(
        f"OpenCode {location} argument at position {position} is outside the "
        "supported option grammar; positional prompts are not supported."
    )


def _validate_model_selection(options: tuple[str, ...], model: str | None) -> None:
    if model is not None:
        _validate_value("--model", model)
    if len(options) > 1:
        raise ValueError("OpenCode accepts at most one raw --model/-m selector.")
    if options and model is not None:
        raise ValueError(
            f"OpenCode option {options[0]!r} conflicts with the resolved Crewplane model."
        )


def build_opencode_command(request: CliInvocationRequest, prompt: str) -> CliCommand:
    """Bind native directory selection to runtime cwd instead of inherited PWD."""
    directory = (request.working_directory or Path.cwd()).resolve()
    return build_standard_command(
        request, prompt, structured_args=("--format", "json", "--dir", str(directory))
    )


OPENCODE = CliProviderCapability(
    provider_kind=ProviderKind.OPENCODE,
    validate_request=validate_opencode_request,
    build_command=build_opencode_command,
    output_extractor=extract_opencode_output,
    usage_decoder=decode_opencode_usage,
    log_presentation_format="json_lines",
    log_presentation_profile="generic",
)
