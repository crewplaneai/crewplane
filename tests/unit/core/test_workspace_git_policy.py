from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.core.workspace.git_policy import (
    WORKSPACE_GIT_BASE_ENVIRONMENT,
    WORKSPACE_GIT_DETERMINISTIC_COMMIT_ENVIRONMENT,
    deterministic_workspace_commit_environment,
    sanitized_workspace_git_environment,
)


def test_sanitized_workspace_git_environment_removes_inherited_git_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/outside/git-dir")
    monkeypatch.setenv("GIT_WORK_TREE", "/outside/work-tree")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/tmp/hooks")
    monkeypatch.setenv("GIT_GLOB_PATHSPECS", "1")

    index_path = tmp_path / "operation.index"
    env = sanitized_workspace_git_environment(index_path)

    assert env["GIT_INDEX_FILE"] == index_path.as_posix()
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env
    assert "GIT_GLOB_PATHSPECS" not in env
    for key, value in WORKSPACE_GIT_BASE_ENVIRONMENT:
        assert env[key] == value


def test_deterministic_workspace_commit_environment_extends_sanitized_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_WORK_TREE", "/outside/work-tree")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "ambient author")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "1999-01-01T00:00:00+0000")

    env = deterministic_workspace_commit_environment()

    assert "GIT_WORK_TREE" not in env
    for key, value in WORKSPACE_GIT_BASE_ENVIRONMENT:
        assert env[key] == value
    for key, value in WORKSPACE_GIT_DETERMINISTIC_COMMIT_ENVIRONMENT:
        assert env[key] == value
