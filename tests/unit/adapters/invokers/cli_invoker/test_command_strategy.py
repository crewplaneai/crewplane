from pathlib import Path
from unittest.mock import Mock

import pytest

from crewplane.adapters.invokers.cli import inspect_cli_command
from crewplane.adapters.invokers.cli_invoker import command_strategy
from crewplane.adapters.invokers.cli_invoker.capabilities import (
    build_cli_invocation_plan,
)
from crewplane.adapters.invokers.cli_invoker.command_posix import PosixCommandStrategy
from crewplane.adapters.invokers.cli_invoker.command_windows import (
    WindowsCommandStrategy,
)
from crewplane.core.config import AgentConfig
from tests.helpers.platforms import requires_posix


@pytest.mark.parametrize("windows", [pytest.param(False, marks=requires_posix), True])
def test_execution_selects_once_and_resolves_against_current_environment(
    tmp_path: Path, monkeypatch, windows: bool
) -> None:
    factory = WindowsCommandStrategy if windows else PosixCommandStrategy
    selector = Mock(side_effect=factory)
    monkeypatch.setattr(command_strategy, "command_strategy", selector)
    executable_name = "agent.exe" if windows else "agent"
    for name in ("preflight", "execution"):
        directory = tmp_path / name
        directory.mkdir()
        executable = directory / executable_name
        executable.touch()
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "preflight"))
    lookup = Mock(return_value=str(tmp_path / "preflight" / executable_name))

    assert inspect_cli_command(["agent"], tmp_path, lookup).available
    assert selector.call_count == 1
    monkeypatch.setenv("PATH", str(tmp_path / "execution"))
    config = AgentConfig(cli_cmd=["agent"], prompt_transport="stdin")
    plan = build_cli_invocation_plan(
        config, None, "exact\r\ninput", tmp_path / "answer", working_directory=tmp_path
    )

    assert selector.call_count == 2
    assert plan.cmd == [str(tmp_path / "execution" / executable_name)]
    assert plan.stdin_data == b"exact\r\ninput"
    assert not (tmp_path / "answer").exists()
