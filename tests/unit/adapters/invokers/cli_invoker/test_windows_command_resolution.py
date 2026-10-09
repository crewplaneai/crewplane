import base64
from pathlib import Path
from unittest.mock import Mock

import pytest

from crewplane.adapters.invokers.cli_invoker.command_types import ResolvedCommand
from crewplane.adapters.invokers.cli_invoker.command_windows import resolve_command
from crewplane.adapters.invokers.cli_invoker.windows_launchers import (
    prepare_windows_launcher,
)


def touch(root: Path, *names: str) -> None:
    root.mkdir(exist_ok=True)
    for name in names:
        (root / name).touch()


@pytest.mark.parametrize(
    "extensions, expected, kind, shell",
    [
        (".EXE;.CMD", "agent.exe", "native", None),
        (".COM;.EXE", "agent.com", "native", None),
        (".CMD;.EXE", "agent.cmd", "batch", "cmd.exe"),
        (".BAT;.EXE", "agent.bat", "batch", "cmd.exe"),
        (".PS1;.EXE", "agent.ps1", "powershell", "pwsh.exe"),
    ],
)
def test_pathext_order_and_required_shells(
    tmp_path, extensions, expected, kind, shell
) -> None:
    touch(
        tmp_path,
        "agent.exe",
        "agent.com",
        "agent.cmd",
        "agent.bat",
        "agent.ps1",
        "cmd.exe",
        "pwsh.exe",
        "powershell.exe",
    )
    result = resolve_command(
        "agent", tmp_path, {"PATH": str(tmp_path), "PATHEXT": extensions}
    )
    assert result == ResolvedCommand(
        str(tmp_path / expected), kind, None if shell is None else str(tmp_path / shell)
    )


def test_path_directory_order_precedes_extension_order(tmp_path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    touch(first, "agent.ps1", "powershell.exe")
    touch(second, "agent.exe")
    result = resolve_command(
        "agent", tmp_path, {"PATH": f"{first};{second}", "PATHEXT": ".EXE"}
    )
    assert result == ResolvedCommand(
        str(first / "agent.ps1"), "powershell", str(first / "powershell.exe")
    )


def test_windows_relative_quoted_path_and_case_insensitive_environment(
    tmp_path,
) -> None:
    first, second = tmp_path / "first space", tmp_path / "second"
    touch(tmp_path, "agent.exe")
    touch(first, "agent.cmd", "agent.py")
    touch(second, "agent.exe", "cmd.exe")
    (first / "agent.exe").mkdir()
    environment = {"Path": ';"first space";second', "PathExt": ".PY;.EXE;.CMD;.cmd"}
    lookup = Mock()

    result = resolve_command("agent", tmp_path, environment, lookup)

    assert result == ResolvedCommand(
        str(first / "agent.cmd"), "batch", str(second / "cmd.exe")
    )
    assert environment == {
        "Path": ';"first space";second',
        "PathExt": ".PY;.EXE;.CMD;.cmd",
    }
    lookup.assert_not_called()


def test_windows_defaults_use_process_cwd_and_environment(
    tmp_path, monkeypatch
) -> None:
    touch(tmp_path, "agent.cmd", "cmd.exe")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("PATHEXT", raising=False)

    assert resolve_command("agent") == ResolvedCommand(
        str(tmp_path / "agent.cmd"), "batch", str(tmp_path / "cmd.exe")
    )


def test_explicit_and_relative_paths_select_requested_launcher(tmp_path) -> None:
    location = tmp_path / "spaces 日本語"
    touch(location, "agent.cmd", "agent.exe", "cmd.exe")
    env = {"PATH": str(location), "PATHEXT": ".EXE;.CMD"}
    assert resolve_command("spaces 日本語/agent.cmd", tmp_path, env).kind == "batch"
    assert resolve_command(str(location / "agent.exe"), tmp_path, env).kind == "native"


def test_windows_explicit_suffix_is_case_insensitive_and_bypasses_pathext(
    tmp_path,
) -> None:
    touch(tmp_path, "agent.EXE")

    assert resolve_command("./agent.EXE", tmp_path, {"PATHEXT": ".CMD"}) == (
        ResolvedCommand(str(tmp_path / "agent.EXE"))
    )


@pytest.mark.parametrize("name", ["missing", "agent.cmd", "agent.ps1", "agent.py"])
def test_missing_commands_unsupported_launchers_and_missing_shells_fail(
    tmp_path, name
) -> None:
    touch(tmp_path, "agent.cmd", "agent.ps1", "agent.py")
    with pytest.raises(ValueError) as caught:
        resolve_command(name, tmp_path, {"PATH": str(tmp_path)})
    if name == "agent.ps1":
        assert str(caught.value) == (
            f"PowerShell is required for '{tmp_path / name}'; install pwsh or configure another launcher."
        )
        assert isinstance(caught.value.__cause__, ValueError)
        assert str(caught.value.__cause__) == (
            "Windows CLI or required shell 'powershell.exe' not found using invocation cwd, PATH and PATHEXT."
        )
    elif name == "agent.py":
        assert str(caught.value) == (
            f"Unsupported Windows launcher '{tmp_path / name}'. Configure an .exe, .com, .cmd, .bat, or .ps1 command."
        )
        assert caught.value.__cause__ is None
    else:
        missing = "cmd.exe" if name == "agent.cmd" else name
        assert str(caught.value) == (
            f"Windows CLI or required shell '{missing}' not found using invocation cwd, PATH and PATHEXT."
        )
        assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "argument",
    ["%PATH%", "bang!", "line\nline", "\x00", 'quote" &side-effect', "x" * 7001],
)
def test_unsafe_batch_arguments_fail_before_launch(argument) -> None:
    with pytest.raises(ValueError, match="Batch|NUL"):
        prepare_windows_launcher(
            ResolvedCommand("agent.cmd", "batch", "cmd.exe"), [argument]
        )


def test_powershell_literals_do_not_become_code() -> None:
    values = ["", "quote'quote", '"', "日本語", "$env:PATH; & whoami", "a\nb"]
    command = prepare_windows_launcher(
        ResolvedCommand("C:/with spaces/agent.ps1", "powershell", "pwsh.exe"), values
    )
    script = base64.b64decode(command[-1]).decode("utf-16le")
    assert "'quote''quote'" in script
    assert "'$env:PATH; & whoami'" in script
    assert "$crewplaneArguments=@(" in script
    assert "& 'C:/with spaces/agent.ps1' @crewplaneArguments;" in script
    assert "-ExecutionPolicy" not in command


def test_native_command_preserves_arguments() -> None:
    values = ["", 'quote"', "%PATH%!", "日本語", "a\nb"]
    assert prepare_windows_launcher(ResolvedCommand("agent.exe"), values) == [
        "agent.exe",
        *values,
    ]
