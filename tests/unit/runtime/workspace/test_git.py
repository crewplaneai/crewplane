from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from orchestrator_cli.runtime.workspace.git import (
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
