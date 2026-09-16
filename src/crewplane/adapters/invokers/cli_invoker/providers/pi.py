from __future__ import annotations

from functools import partial

from crewplane.architecture.contracts import ProviderKind

from ..capability import CliInvocationRequest, CliProviderCapability
from ..commands import build_standard_command
from ..env_command import parse_env_command_context
from ..streaming import extract_strict_stdout
from ..validation import reject_unsupported_reasoning

_CONFLICTING_OPTIONS = frozenset(
    {
        "--continue",
        "-c",
        "--resume",
        "-r",
        "--session",
        "--session-id",
        "--session-dir",
        "--fork",
        "--name",
        "-n",
        "--no-approve",
        "-na",
        "--help",
        "-h",
        "--version",
        "-v",
        "--export",
        "--list-models",
    }
)
_VALUE_OPTIONS = frozenset(
    {
        "--provider",
        "--model",
        "--api-key",
        "--system-prompt",
        "--append-system-prompt",
        "--models",
        "--tools",
        "-t",
        "--exclude-tools",
        "-xt",
        "--thinking",
        "--extension",
        "-e",
        "--skill",
        "--prompt-template",
        "--theme",
        "--use-theme",
        "--tui-mode",
    }
)


def validate_pi_request(request: CliInvocationRequest) -> None:
    reject_unsupported_reasoning(request)
    config = request.config
    if config.prompt_transport != "stdin" or config.prompt_transport_arg is not None:
        raise ValueError("Pi requires stdin transport without prompt_transport_arg.")
    context = parse_env_command_context(config.cli_cmd, None, "")
    if context.command_executable is None:
        raise ValueError("Pi requires an executable after the env wrapper.")
    _validate_arguments(context.command_arguments)
    _validate_arguments(tuple(config.extra_args))


def _validate_arguments(arguments: tuple[str, ...]) -> None:
    if arguments and arguments[0] in {
        "auth",
        "config",
        "install",
        "remove",
        "uninstall",
        "update",
        "list",
    }:
        raise ValueError("Pi management subcommands conflict with managed text mode.")
    if "--" in arguments:
        raise ValueError(
            "Pi managed flags cannot follow a configured option separator."
        )
    tokens = iter(arguments)
    for token in tokens:
        option, separator, _ = token.partition("=")
        if option in _CONFLICTING_OPTIONS:
            raise ValueError(f"Pi option {option!r} conflicts with managed text mode.")
        if option == "--mode":
            if separator or next(tokens, None) != "text":
                raise ValueError("Pi requires --mode text in split argument form.")
        elif option in _VALUE_OPTIONS and not separator and next(tokens, None) is None:
            raise ValueError(f"Pi option {option!r} requires a value.")


PI = CliProviderCapability(
    provider_kind=ProviderKind.PI,
    validate_request=validate_pi_request,
    build_command=partial(
        build_standard_command,
        structured_args=("--print", "--mode", "text", "--no-session", "--approve"),
    ),
    output_extractor=extract_strict_stdout,
    supports_output_idle_timeout=False,
)
