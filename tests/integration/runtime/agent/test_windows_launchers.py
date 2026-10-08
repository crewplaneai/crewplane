import asyncio
import json
import os
import sys

import pytest

from crewplane.adapters.invokers.cli_invoker.command_resolution import resolve_command
from crewplane.adapters.invokers.cli_invoker.windows_launchers import (
    prepare_windows_launcher,
)
from crewplane.runtime.agent.invocation.command import run_command_once

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Requires native Windows shell parsing"
)


@pytest.mark.parametrize("suffix", [".cmd", ".bat", ".ps1"])
@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["only argument"],
        ["", "space value", "日本語", "a&b", "x|y", "<input>", "a^b", "(literal)"],
        ['quote"value', "", "trailing\\"],
    ],
)
def test_shell_round_trip_preserves_literals_stdin_and_exit(
    tmp_path, monkeypatch, suffix, arguments
) -> None:
    directory = tmp_path / "launcher space 日本語"
    directory.mkdir()
    launcher = directory / ("probe" + suffix)
    probe = directory / "probe.py"
    probe.write_text(
        "import json,sys; print(json.dumps({'args':sys.argv[1:],'count':len(sys.argv)-1,'stdin':sys.stdin.buffer.read().hex()},ensure_ascii=True));sys.exit(7)",
        encoding="utf-8",
    )
    if suffix == ".ps1":
        launcher.write_text(
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
            "$inputBytes = New-Object System.IO.MemoryStream\n"
            "[Console]::OpenStandardInput().CopyTo($inputBytes)\n"
            "$hex = ([System.BitConverter]::ToString($inputBytes.ToArray())).Replace('-', '').ToLower()\n"
            "@{args=@($args);count=$args.Count;stdin=$hex} | ConvertTo-Json -Compress\nexit 7\n",
            encoding="utf-8",
        )
    else:
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "%~dp0probe.py" %*\r\nexit /b %errorlevel%\r\n',
            encoding="utf-8",
            newline="",
        )
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    resolved = resolve_command(str(launcher), directory)
    command = prepare_windows_launcher(resolved, arguments)
    payload = "prompt\r\nCtrl-Z\x1a café".encode()
    result = asyncio.run(
        run_command_once(command, payload, None, False, None, directory, None, None)
    )
    try:
        assert result.returncode == 7, result.stderr_path.read_text()
        assert json.loads(result.stdout_path.read_bytes()) == {
            "args": arguments,
            "count": len(arguments),
            "stdin": payload.hex(),
        }
        assert sorted(path.name for path in directory.iterdir()) == [
            "probe" + suffix,
            "probe.py",
        ]
    finally:
        result.stdout_path.unlink()
        result.stderr_path.unlink()


def test_powershell_execution_policy_is_not_bypassed(tmp_path, monkeypatch) -> None:
    launcher = tmp_path / "blocked.ps1"
    launcher.write_text("Set-Content unexpected 'started'", encoding="utf-8")
    monkeypatch.setenv("PSExecutionPolicyPreference", "Restricted")
    resolved = resolve_command(str(launcher), tmp_path)
    result = asyncio.run(
        run_command_once(
            prepare_windows_launcher(resolved, []),
            None,
            None,
            False,
            None,
            tmp_path,
            None,
            None,
        )
    )
    assert result.returncode != 0
    assert not (tmp_path / "unexpected").exists()
    result.stdout_path.unlink()
    result.stderr_path.unlink()
