import base64
import sys

import pytest

from crewplane.adapters.invokers.cli_invoker.command_types import ResolvedCommand
from crewplane.adapters.invokers.cli_invoker.windows_launchers import (
    prepare_windows_launcher,
    validate_batch_arguments,
)

_UNSAFE_BATCH_ERROR = (
    "Batch launchers cannot safely transport NUL, newlines, % or ! in arguments. "
    "Use stdin prompt transport or an .exe/.ps1 launcher."
)
_BATCH_QUOTES_ERROR = (
    "Batch launchers cannot combine embedded quotes with shell metacharacters. "
    "Use stdin prompt transport or an .exe/.ps1 launcher."
)
_BATCH_BUDGET_ERROR = (
    "Batch launcher arguments exceed the safe cmd command-line budget. "
    "Use stdin prompt transport or an .exe/.ps1 launcher."
)


def test_native_launcher_returns_new_argv_without_mutating_arguments() -> None:
    arguments = ["", 'quote"', "%PATH%!", "日本語", "a\nb", "trailing\\"]
    original = arguments.copy()

    command = prepare_windows_launcher(ResolvedCommand("agent.exe"), arguments)

    assert command == ["agent.exe", *original]
    assert arguments == original
    command.append("new")
    assert arguments == original


@pytest.mark.parametrize("kind", ["native", "batch", "powershell"])
def test_nul_arguments_fail_before_shell_validation(kind) -> None:
    arguments = ["bad\x00value"]
    with pytest.raises(ValueError) as caught:
        prepare_windows_launcher(ResolvedCommand("agent", kind), arguments)

    assert str(caught.value) == "Windows launcher arguments cannot contain NUL bytes."
    assert arguments == ["bad\x00value"]


@pytest.mark.parametrize("kind", ["batch", "powershell"])
def test_missing_shell_fails_before_batch_argument_validation(kind) -> None:
    with pytest.raises(ValueError) as caught:
        prepare_windows_launcher(ResolvedCommand("agent", kind), ["%PATH%"])

    assert str(caught.value) == "Required shell is missing for agent."


@pytest.mark.parametrize(
    "arguments, literals",
    [
        ([], ""),
        (
            [
                "",
                "quote'quote",
                '"',
                "日本語",
                "$env:PATH; & whoami",
                "a\nb",
                "trailing\\",
            ],
            "'','quote''quote','\"','日本語','$env:PATH; & whoami','a\nb','trailing\\'",
        ),
    ],
)
def test_powershell_launcher_preserves_exact_script_and_argv(
    arguments, literals
) -> None:
    original = arguments.copy()
    expected_script = (
        "$ErrorActionPreference='Stop'; "
        f"$crewplaneArguments=@({literals}); "
        "& 'C:/launcher space 日本語/agent''s.ps1' @crewplaneArguments; "
        "$ok=$?; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }; if (-not $ok) { exit 1 }"
    )

    command = prepare_windows_launcher(
        ResolvedCommand(
            "C:/launcher space 日本語/agent's.ps1", "powershell", "pwsh.exe"
        ),
        arguments,
    )

    assert command == [
        "pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        base64.b64encode(expected_script.encode("utf-16le")).decode("ascii"),
    ]
    assert arguments == original


@pytest.mark.parametrize(
    "arguments, literals",
    [
        ([], ""),
        (
            [
                "",
                "space value",
                "日本語",
                "a&b",
                "x|y",
                "<input>",
                "a^b",
                "(literal)",
                "trailing\\",
                "space trailing\\",
                "double\\\\",
            ],
            ' "" "space value" "日本語" "a&b" "x|y" "<input>" "a^b" "(literal)"'
            ' "trailing\\\\" "space trailing\\\\" "double\\\\\\\\"',
        ),
        (
            ['quote"value', "", "trailing\\"],
            ' "quote\\"value" "" "trailing\\\\"',
        ),
    ],
)
def test_batch_launcher_preserves_exact_command_line_and_transport(
    arguments, literals
) -> None:
    original = arguments.copy()
    shell = "C:/shell space/cmd.exe"
    expected_command_line = (
        f'"{shell}" /d /s /v:off /c ""C:/launcher space 日本語/agent.cmd"{literals}"'
    )

    command = prepare_windows_launcher(
        ResolvedCommand("C:/launcher space 日本語/agent.cmd", "batch", shell), arguments
    )

    assert command == [
        sys.executable,
        "-m",
        "crewplane.adapters.invokers.cli_invoker.windows_shell_transport",
        shell,
        base64.b64encode(expected_command_line.encode("utf-8")).decode("ascii"),
    ]
    assert arguments == original


@pytest.mark.parametrize("character", ["\x00", "\r", "\n", "%", "!"])
@pytest.mark.parametrize("value_index", [0, 1])
def test_batch_validation_rejects_unsafe_executable_or_argument(
    character, value_index
) -> None:
    values = ["agent.cmd", "argument"]
    values[value_index] += character
    with pytest.raises(ValueError) as caught:
        validate_batch_arguments(values)

    assert str(caught.value) == _UNSAFE_BATCH_ERROR


@pytest.mark.parametrize(
    "values, expected_error",
    [
        (["agent.cmd", "%PATH%", 'quote"', "&" * 7000], _UNSAFE_BATCH_ERROR),
        (["agent.cmd", 'quote"', "&" * 7000], _BATCH_QUOTES_ERROR),
        (['agent".cmd', "&"], _BATCH_QUOTES_ERROR),
        (["agent&.cmd", 'quote"'], _BATCH_QUOTES_ERROR),
    ],
)
def test_batch_validation_preserves_error_order_and_checks_across_values(
    values, expected_error
) -> None:
    with pytest.raises(ValueError) as caught:
        prepare_windows_launcher(
            ResolvedCommand(values[0], "batch", "cmd.exe"), values[1:]
        )

    assert str(caught.value) == expected_error


@pytest.mark.parametrize(
    "argument",
    ["x" * 6986, "😀" * 3493, "x" * 6984 + "\\"],
    ids=["ascii", "surrogate-pairs", "trailing-backslash"],
)
def test_batch_budget_counts_quoted_utf16_units_including_executable(argument) -> None:
    values = ["agent.cmd", argument]
    original = values.copy()

    assert validate_batch_arguments(values) is None
    assert values == original
    with pytest.raises(ValueError) as caught:
        validate_batch_arguments(["agent.cmd", "a" + argument])

    assert str(caught.value) == _BATCH_BUDGET_ERROR


@pytest.mark.parametrize("kind", ["batch", "powershell"])
def test_shell_launcher_propagates_encoding_errors(kind) -> None:
    with pytest.raises(UnicodeEncodeError):
        prepare_windows_launcher(
            ResolvedCommand("agent", kind, "shell.exe"), ["\ud800"]
        )
