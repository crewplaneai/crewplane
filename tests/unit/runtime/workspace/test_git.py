from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from crewplane.core.workspace.git_policy import workspace_git_config_args
from crewplane.runtime.workspace.git import (
    RUNTIME_GIT_COMMAND_TIMEOUT_SECONDS,
    GitCommand,
    git,
)


def test_git_command_run_uses_bounded_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"ok\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = git(tmp_path).run("status", "--short")

    assert result.stdout == b"ok\n"
    assert calls[0]["timeout"] == RUNTIME_GIT_COMMAND_TIMEOUT_SECONDS


def test_git_command_run_with_input_uses_bounded_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"abc123\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitCommand(cwd=tmp_path, env={}).run_with_input(
        b"message",
        "commit-tree",
        "abc123",
    )

    assert result.stdout == b"abc123\n"
    assert calls[0]["input"] == b"message"
    assert calls[0]["timeout"] == RUNTIME_GIT_COMMAND_TIMEOUT_SECONDS


def test_commit_exists_uses_runtime_git_transport(tmp_path: Path) -> None:
    command = git(tmp_path)
    with patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
    ) as run:
        assert command.commit_exists("abc123") is True

    run.assert_called_once_with(
        [
            "git",
            *workspace_git_config_args(),
            "-C",
            tmp_path.as_posix(),
            "cat-file",
            "-e",
            "abc123^{commit}",
        ],
        check=True,
        capture_output=True,
        env=command.env,
        input=None,
        timeout=600.0,
    )


@pytest.mark.parametrize("returncode", [1, 128, -9])
def test_commit_exists_returns_false_for_every_git_exit_failure(
    tmp_path: Path, returncode: int
) -> None:
    with patch(
        "subprocess.run", side_effect=subprocess.CalledProcessError(returncode, "git")
    ):
        assert git(tmp_path).commit_exists("abc123") is False


@pytest.mark.parametrize(
    "error", [subprocess.TimeoutExpired("git", 600), OSError("cannot execute git")]
)
def test_commit_exists_propagates_transport_errors(
    tmp_path: Path, error: Exception
) -> None:
    with (
        patch("subprocess.run", side_effect=error),
        pytest.raises(type(error)) as caught,
    ):
        git(tmp_path).commit_exists("abc123")
    assert caught.value is error
