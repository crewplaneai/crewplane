from __future__ import annotations

import pytest

from crewplane.adapters.invokers.cli_invoker.env_command import (
    EnvCommandContext,
    parse_env_command_context,
)

_TRACKED_ENVIRONMENT = "TRACKED_ENVIRONMENT"


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        pytest.param(
            [],
            EnvCommandContext(
                command_executable=None,
                command_arguments=(),
                tracked_environment_value="inherited",
                tracked_environment_changed=False,
                command_search_path=None,
                command_working_directory=None,
                command_working_directory_option=None,
            ),
            id="empty-command",
        ),
        pytest.param(
            ["codex", "exec"],
            EnvCommandContext(
                command_executable="codex",
                command_arguments=("exec",),
                tracked_environment_value="inherited",
                tracked_environment_changed=False,
                command_search_path=None,
                command_working_directory=None,
                command_working_directory_option=None,
            ),
            id="direct-command",
        ),
    ],
)
def test_parse_env_command_context_preserves_unwrapped_commands(
    tokens: list[str],
    expected: EnvCommandContext,
) -> None:
    assert (
        parse_env_command_context(tokens, "inherited", _TRACKED_ENVIRONMENT) == expected
    )


def test_parse_env_command_context_consumes_assignments_before_command() -> None:
    context = parse_env_command_context(
        [
            "env",
            "OTHER=value",
            f"{_TRACKED_ENVIRONMENT}=first",
            f"{_TRACKED_ENVIRONMENT}=second",
            "claude",
            f"{_TRACKED_ENVIRONMENT}=argument",
        ],
        "inherited",
        _TRACKED_ENVIRONMENT,
    )

    assert context == EnvCommandContext(
        command_executable="claude",
        command_arguments=(f"{_TRACKED_ENVIRONMENT}=argument",),
        tracked_environment_value="second",
        tracked_environment_changed=True,
        command_search_path=None,
        command_working_directory=None,
        command_working_directory_option=None,
    )


def test_parse_env_command_context_applies_short_option_state() -> None:
    context = parse_env_command_context(
        [
            "env",
            "-ivPfirst-bin",
            "-P",
            "second-bin",
            "-Cfirst-worktree",
            "-C",
            "second-worktree",
            f"{_TRACKED_ENVIRONMENT}=configured",
            "provider",
        ],
        "inherited",
        _TRACKED_ENVIRONMENT,
    )

    assert context == EnvCommandContext(
        command_executable="provider",
        command_arguments=(),
        tracked_environment_value="configured",
        tracked_environment_changed=True,
        command_search_path="second-bin",
        command_working_directory="second-worktree",
        command_working_directory_option="-C",
    )


def test_parse_env_command_context_applies_long_option_state() -> None:
    context = parse_env_command_context(
        [
            "/usr/bin/env",
            "--ignore-environment",
            "--unset=OTHER",
            "--argv0=alias",
            "--chdir=first-worktree",
            "--chdir",
            "second-worktree",
            "--debug",
            f"{_TRACKED_ENVIRONMENT}=configured",
            "provider",
        ],
        "inherited",
        _TRACKED_ENVIRONMENT,
    )

    assert context == EnvCommandContext(
        command_executable="provider",
        command_arguments=(),
        tracked_environment_value="configured",
        tracked_environment_changed=True,
        command_search_path=None,
        command_working_directory="second-worktree",
        command_working_directory_option="--chdir",
    )


def test_parse_env_command_context_consumes_reset_after_option_terminator() -> None:
    context = parse_env_command_context(
        ["env", "--", "-", f"{_TRACKED_ENVIRONMENT}=configured", "provider"],
        "inherited",
        _TRACKED_ENVIRONMENT,
    )

    assert context == EnvCommandContext(
        command_executable="provider",
        command_arguments=(),
        tracked_environment_value="configured",
        tracked_environment_changed=True,
        command_search_path=None,
        command_working_directory=None,
        command_working_directory_option=None,
    )


def test_parse_env_command_context_consumes_option_looking_values() -> None:
    context = parse_env_command_context(
        ["env", "--argv0", "--provider-alias", "provider"],
        "inherited",
        _TRACKED_ENVIRONMENT,
    )

    assert context.command_executable == "provider"
    assert context.command_arguments == ()


@pytest.mark.parametrize(
    ("tokens", "message"),
    [
        pytest.param(
            ["env", "--unknown", "provider"],
            "Cannot validate env option '--unknown' with a workflow reasoning request.",
            id="unknown-long-option",
        ),
        pytest.param(
            ["env", "-vx", "provider"],
            "Cannot validate env option '-x' with a workflow reasoning request.",
            id="unknown-short-option",
        ),
        pytest.param(
            ["env", "--split-string=value", "provider"],
            "--split-string cannot be combined with a workflow reasoning request.",
            id="split-string",
        ),
        pytest.param(
            ["env", "-S"],
            "-S requires a value.",
            id="split-string-missing-value",
        ),
        pytest.param(
            ["env", "-Svalue", "provider"],
            "-S cannot be combined with a workflow reasoning request.",
            id="short-split-string",
        ),
        pytest.param(
            ["env", "--unset"],
            "--unset requires a value.",
            id="unset-missing-value",
        ),
    ],
)
def test_parse_env_command_context_preserves_validation_errors(
    tokens: list[str],
    message: str,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_env_command_context(tokens, "inherited", _TRACKED_ENVIRONMENT)

    assert str(exc_info.value) == message
