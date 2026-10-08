import base64
from pathlib import Path

import pytest

from crewplane.adapters.invokers.cli_invoker.command_resolution import (
    ResolvedCommand,
    resolve_command,
)
from crewplane.adapters.invokers.cli_invoker.windows_launchers import (
    prepare_windows_launcher,
)


@pytest.fixture(autouse=True)
def windows(monkeypatch):
    monkeypatch.setattr(
        "crewplane.adapters.invokers.cli_invoker.command_resolution.is_native_windows",
        lambda: True,
    )


def touch(root: Path, *names: str) -> None:
    root.mkdir(exist_ok=True)
    for name in names:
        (root / name).touch()


@pytest.mark.parametrize(
    "extensions, expected",
    [
        (".EXE;.CMD", "agent.exe"),
        (".CMD;.EXE", "agent.cmd"),
        (".PS1;.EXE", "agent.ps1"),
    ],
)
def test_pathext_order_and_required_shells(tmp_path, extensions, expected) -> None:
    touch(tmp_path, "agent.exe", "agent.cmd", "agent.ps1", "cmd.exe", "pwsh.exe")
    result = resolve_command(
        "agent", tmp_path, {"PATH": str(tmp_path), "PATHEXT": extensions}
    )
    assert Path(result.executable).name == expected


def test_path_directory_order_precedes_extension_order(tmp_path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    touch(first, "agent.ps1", "powershell.exe")
    touch(second, "agent.exe")
    result = resolve_command(
        "agent", tmp_path, {"PATH": f"{first};{second}", "PATHEXT": ".EXE"}
    )
    assert Path(result.executable) == first / "agent.ps1"
    assert Path(result.shell).name == "powershell.exe"


def test_explicit_and_relative_paths_select_requested_launcher(tmp_path) -> None:
    location = tmp_path / "spaces 日本語"
    touch(location, "agent.cmd", "agent.exe", "cmd.exe")
    env = {"PATH": str(location), "PATHEXT": ".EXE;.CMD"}
    assert resolve_command("spaces 日本語/agent.cmd", tmp_path, env).kind == "batch"
    assert resolve_command(str(location / "agent.exe"), tmp_path, env).kind == "native"


@pytest.mark.parametrize("name", ["missing", "agent.cmd", "agent.ps1", "agent.py"])
def test_missing_commands_unsupported_launchers_and_missing_shells_fail(
    tmp_path, name
) -> None:
    touch(tmp_path, "agent.cmd", "agent.ps1", "agent.py")
    with pytest.raises(ValueError):
        resolve_command(name, tmp_path, {"PATH": str(tmp_path)})


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
