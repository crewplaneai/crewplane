from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.artifacts.workspace import bundle_validation
from crewplane.core.preflight.workspace.files import (
    git_reads as workspace_git_file_reads,
)
from crewplane.core.workspace.git_policy import (
    WORKSPACE_GIT_BASE_ENVIRONMENT,
    WORKSPACE_GIT_DETERMINISTIC_COMMIT_ENVIRONMENT,
    WORKSPACE_GIT_ENV_UNSET,
    deterministic_workspace_commit_environment,
    sanitized_workspace_git_environment,
    workspace_git_base_environment,
)
from crewplane.runtime.agent.workspace_environment import workspace_child_environment
from crewplane.runtime.workspace import git as runtime_workspace_git


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


def test_sanitized_workspace_git_environment_removes_every_inherited_git_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in WORKSPACE_GIT_ENV_UNSET:
        monkeypatch.setenv(key, "ambient")

    env = sanitized_workspace_git_environment(read_only=False)
    baseline = workspace_git_base_environment(read_only=False)

    for key in WORKSPACE_GIT_ENV_UNSET:
        if key in baseline:
            assert env[key] == baseline[key]
        else:
            assert key not in env
    assert "GIT_TEMPLATE_DIR" not in env
    assert "GIT_OPTIONAL_LOCKS" not in env


def test_sanitized_workspace_git_environment_removes_dynamic_config_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_KEY_9", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_9", "true")

    env = sanitized_workspace_git_environment()

    assert "GIT_CONFIG_KEY_9" not in env
    assert "GIT_CONFIG_VALUE_9" not in env


def test_workspace_git_base_environment_supports_known_variants(
    tmp_path: Path,
) -> None:
    env = workspace_git_base_environment(
        read_only=True,
        ceiling_directories=tmp_path,
    )

    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_ATTR_NOSYSTEM"] == "1"
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_CEILING_DIRECTORIES"] == tmp_path.as_posix()


def test_sanitized_workspace_git_environment_injects_temporary_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/ambient.index")
    index_path = tmp_path / "operation.index"

    env = sanitized_workspace_git_environment(index_path=index_path)

    assert env["GIT_INDEX_FILE"] == index_path.as_posix()


def test_git_command_call_sites_use_shared_sanitized_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/tmp/wrong-repo")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", "/tmp/template")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    artifact_env = bundle_validation.sanitized_bundle_git_environment()
    preflight_env = workspace_git_file_reads.git_env()
    runtime_env = runtime_workspace_git.sanitized_git_env()

    for env in (artifact_env, preflight_env, runtime_env):
        assert "GIT_DIR" not in env
        assert "GIT_TEMPLATE_DIR" not in env
        assert "GIT_CONFIG_KEY_0" not in env
        assert "GIT_CONFIG_VALUE_0" not in env
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_OPTIONAL_LOCKS"] == "0"


def test_workspace_child_environment_uses_shared_unset_and_ceiling_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_TEMPLATE_DIR", "/tmp/template")
    monkeypatch.setenv("GIT_CONFIG_KEY_4", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_4", "true")

    child = workspace_child_environment(tmp_path / "checkout")

    assert "GIT_TEMPLATE_DIR" in child.unset
    assert "GIT_CONFIG_KEY_4" in child.unset
    assert "GIT_CONFIG_VALUE_4" in child.unset
    assert child.set["GIT_CONFIG_NOSYSTEM"] == "1"
    assert child.set["GIT_CEILING_DIRECTORIES"] == tmp_path.as_posix()
    assert "GIT_OPTIONAL_LOCKS" not in child.set
    assert int(child.set["GIT_CONFIG_COUNT"]) > 0


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
