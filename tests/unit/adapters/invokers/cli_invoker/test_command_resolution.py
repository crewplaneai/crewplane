import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from crewplane.adapters.invokers.cli_invoker.command_posix import resolve_command
from crewplane.adapters.invokers.cli_invoker.command_types import ResolvedCommand
from tests.helpers.platforms import requires_posix

pytestmark = [requires_posix, pytest.mark.usefixtures("posix_cli_plans")]


@pytest.mark.parametrize("search_path", ["missing:tools:", "", None])
def test_posix_search_path_uses_invocation_cwd_and_environment(
    tmp_path, monkeypatch, search_path
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    for directory in (tmp_path, tools):
        executable = directory / "agent"
        executable.touch()
        executable.chmod(0o755)
    monkeypatch.setattr(os, "defpath", "tools")
    monkeypatch.setenv("PATH", "unrelated")
    environment = {} if search_path is None else {"PATH": search_path}
    original_cwd = Path.cwd()

    result = resolve_command("agent", tmp_path, environment)

    expected_parent = tmp_path if search_path == "" else tools
    assert result == ResolvedCommand(str(expected_parent / "agent"))
    assert Path.cwd() == original_cwd
    assert environment == ({} if search_path is None else {"PATH": search_path})
    assert os.environ["PATH"] == "unrelated"


def test_posix_lookup_result_is_returned_without_filesystem_validation(
    tmp_path,
) -> None:
    lookup = Mock(return_value="unvalidated lookup result")

    result = resolve_command("agent", tmp_path, {"PATH": "missing"}, lookup)

    assert result == ResolvedCommand("unvalidated lookup result")
    lookup.assert_called_once_with("agent")


def test_posix_missing_lookup_result_has_exact_diagnostic(tmp_path) -> None:
    lookup = Mock(return_value=None)

    with pytest.raises(ValueError) as caught:
        resolve_command("agent", tmp_path, {}, lookup)

    assert str(caught.value) == "CLI 'agent' not found in PATH."
    lookup.assert_called_once_with("agent")


def test_posix_lookup_exception_propagates_unchanged(tmp_path) -> None:
    failure = OSError("lookup failed")
    lookup = Mock(side_effect=failure)

    with pytest.raises(OSError) as caught:
        resolve_command("agent", tmp_path, {}, lookup)

    assert caught.value is failure
    lookup.assert_called_once_with("agent")


@pytest.mark.parametrize("absolute", [False, True])
def test_posix_explicit_paths_bypass_lookup(tmp_path, absolute) -> None:
    executable = tmp_path / "tools" / "agent"
    executable.parent.mkdir()
    executable.touch()
    executable.chmod(0o755)
    lookup = Mock()
    command = str(executable) if absolute else "tools/agent"

    result = resolve_command(command, tmp_path, {}, lookup)

    assert result == ResolvedCommand(executable.resolve().as_posix())
    lookup.assert_not_called()


def test_posix_defaults_use_process_cwd_and_environment(tmp_path, monkeypatch) -> None:
    executable = tmp_path / "agent"
    executable.touch()
    executable.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")

    assert resolve_command("agent") == ResolvedCommand(str(executable))
