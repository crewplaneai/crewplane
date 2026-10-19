import asyncio
import sys

from crewplane.architecture.contracts import ChildProcessEnvironment
from crewplane.core import agent_environment
from crewplane.runtime.agent.invocation.command import run_command_once
from crewplane.runtime.agent.workspace_environment import (
    with_agent_environment,
    workspace_child_environment,
)


def test_agent_values_cannot_override_workspace_git_environment(tmp_path):
    controlled = workspace_child_environment(tmp_path)
    result = with_agent_environment(
        {
            "OLLAMA_HOST": "http://server:11434",
            "GIT_DIR": "/wrong",
            "GIT_CONFIG_COUNT": "99",
            "GIT_CONFIG_KEY_99": "core.worktree",
            "GIT_CONFIG_VALUE_99": "/wrong",
        },
        controlled,
        True,
    )
    assert result is not None
    assert result.set["OLLAMA_HOST"] == "http://server:11434"
    assert all(result.set[key] == value for key, value in controlled.set.items())
    assert "GIT_DIR" not in result.set
    assert "GIT_CONFIG_KEY_99" not in result.set
    assert "GIT_CONFIG_VALUE_99" not in result.set


def test_explicit_child_environment_takes_precedence():
    result = with_agent_environment(
        {"ADD": "value", "REMOVE": "value", "OVERRIDE": "agent"},
        ChildProcessEnvironment(set={"OVERRIDE": "controlled"}, unset=("REMOVE",)),
    )
    assert result is not None
    assert dict(result.set) == {"ADD": "value", "OVERRIDE": "controlled"}
    assert result.unset == ("REMOVE",)


def test_controlled_removals_are_case_insensitive_on_windows(monkeypatch):
    monkeypatch.setattr(agent_environment, "is_native_windows", lambda: True)
    result = with_agent_environment(
        {"REMOVE": "agent"}, ChildProcessEnvironment(set={}, unset=("remove",))
    )
    assert result is not None
    assert dict(result.set) == {}
    assert result.unset == ("REMOVE",)


def test_launch_environment_normalizes_windows_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("CREWPLANE_ENV_TEST", "inherited")
    monkeypatch.setattr(agent_environment, "is_native_windows", lambda: True)
    result = asyncio.run(
        run_command_once(
            cmd=[
                sys.executable,
                "-c",
                "import os; print(os.environ['CREWPLANE_ENV_TEST'])",
            ],
            stdin_data=None,
            log_file=None,
            append_log=False,
            log_header=None,
            cwd=tmp_path,
            invocation_context=None,
            idle_timeout_seconds=None,
            child_environment=ChildProcessEnvironment(
                set={"crewplane_env_test": "configured"}, unset=()
            ),
        )
    )
    try:
        assert result.returncode == 0
        assert result.stdout_text.strip() == "configured"
    finally:
        if result.stdout_path is not None:
            result.stdout_path.unlink(missing_ok=True)
        if result.stderr_path is not None:
            result.stderr_path.unlink(missing_ok=True)
