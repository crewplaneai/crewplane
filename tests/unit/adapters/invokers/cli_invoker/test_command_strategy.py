import os
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


@requires_posix
@pytest.mark.usefixtures("posix_cli_plans")
@pytest.mark.parametrize("search_path", ["tools", ""])
def test_inherited_relative_path_uses_project_for_availability_and_workspace_for_execution(
    tmp_path: Path, monkeypatch, search_path: str
) -> None:
    project = tmp_path / "project"
    caller = project / "child"
    workspace = tmp_path / "workspace"
    for directory in (project, caller, workspace):
        executable = directory / search_path / "agent"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        executable.chmod(0o755)
    monkeypatch.chdir(caller)
    monkeypatch.setenv("PATH", search_path)
    environment = dict(os.environ)
    lookup = Mock(side_effect=AssertionError("ambient lookup must not be used"))
    strategy = PosixCommandStrategy()

    assert strategy.availability(["agent"], project, lookup).available
    (project / search_path / "agent").unlink()
    assert not strategy.availability(["agent"], project, lookup).available
    config = AgentConfig(cli_cmd=["agent", "unchanged/path"], prompt_transport="stdin")
    plan = build_cli_invocation_plan(
        config, None, "prompt", tmp_path / "answer", working_directory=workspace
    )

    assert plan.cmd == [str(workspace / search_path / "agent"), "unchanged/path"]
    assert Path.cwd() == caller
    assert dict(os.environ) == environment
    assert config.environment == {}
    lookup.assert_not_called()


@requires_posix
@pytest.mark.usefixtures("posix_cli_plans")
@pytest.mark.parametrize("search_path", ["tools", ""])
def test_relative_path_missing_bare_command_keeps_launch_handoff(
    tmp_path: Path, monkeypatch, search_path: str
) -> None:
    monkeypatch.setenv("PATH", search_path)
    config = AgentConfig(cli_cmd=["missing-provider"], prompt_transport="stdin")

    plan = build_cli_invocation_plan(
        config, None, "prompt", tmp_path / "answer", working_directory=tmp_path
    )

    assert plan.cmd == ["missing-provider"]


@requires_posix
@pytest.mark.usefixtures("posix_cli_plans")
def test_configured_path_overrides_relative_inherited_path(tmp_path: Path, monkeypatch):
    configured = tmp_path / "configured"
    configured.mkdir()
    executable = configured / "agent"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", "missing")
    config = AgentConfig(
        cli_cmd=["agent"], environment={"PATH": "configured"}, prompt_transport="stdin"
    )
    lookup = Mock(side_effect=AssertionError("inherited lookup must not be used"))

    assert (
        PosixCommandStrategy()
        .availability(config.cli_cmd, tmp_path, lookup, config.environment)
        .available
    )
    plan = build_cli_invocation_plan(
        config, None, "prompt", tmp_path / "answer", working_directory=tmp_path
    )

    assert plan.cmd == [str(executable)]
    assert config.environment == {"PATH": "configured"}
    lookup.assert_not_called()
