from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.cli.cleanup import cleanup_repository_id
from crewplane.version import SCHEMA_VERSION


def test_cleanup_workspaces_defaults_to_advisory_dry_run(tmp_path: Path) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix()],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Would remove 1 workspace path(s)" in result.output
    assert workspace_path.exists()
    assert project_root.exists()


def test_cleanup_workspaces_yes_removes_paths(tmp_path: Path) -> None:
    _, config_path, workspace_path = _cleanup_project(tmp_path, initialize_git=True)

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 1 workspace path(s)" in result.output
    assert not workspace_path.exists()


def test_cleanup_workspaces_yes_removes_run_owned_refs(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    _git(project_root, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 1 workspace path(s)" in result.output
    assert "Removed 1 run-owned Git ref(s)" in result.output
    assert not workspace_path.exists()
    assert _git(project_root, "for-each-ref", "refs/crewplane/runs/run-1") == ""


def test_cleanup_workspaces_yes_ignores_symlink_workspace_candidates(
    tmp_path: Path,
) -> None:
    _, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
        create_workspace=False,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    workspace_path.parent.mkdir(parents=True)
    try:
        workspace_path.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 0 workspace path(s)" in result.output
    assert workspace_path.is_symlink()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_cleanup_workspaces_all_projects_allows_non_git_project(
    tmp_path: Path,
) -> None:
    _, config_path, workspace_path = _cleanup_project(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            "--all-projects",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Would remove 1 workspace path(s)" in result.output
    assert "status=unknown" in result.output
    assert "status=orphan" not in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_all_projects_rejects_orphan_filter(
    tmp_path: Path,
) -> None:
    _, config_path, workspace_path = _cleanup_project(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            "--all-projects",
            "--orphans",
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "--all-projects cannot be combined" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_default_requires_git_project(tmp_path: Path) -> None:
    _, config_path, _ = _cleanup_project(tmp_path)

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix()],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Use --all-projects" in result.output


def test_cleanup_workspaces_rejects_relative_cache_root(tmp_path: Path) -> None:
    _, config_path, workspace_path = _cleanup_project(tmp_path, initialize_git=True)
    config_text = config_path.read_text(encoding="utf-8")
    cache_root = workspace_path.parents[3]
    config_path.write_text(
        config_text.replace(cache_root.as_posix(), "relative-cache"),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "cache_root must be absolute" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_rejects_project_cache_root(tmp_path: Path) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    config_text = config_path.read_text(encoding="utf-8")
    cache_root = workspace_path.parents[3]
    config_path.write_text(
        config_text.replace(cache_root.as_posix(), project_root.as_posix()),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "must not overlap" in result.output
    assert project_root.exists()


def _cleanup_project(
    tmp_path: Path,
    initialize_git: bool = False,
    create_workspace: bool = True,
) -> tuple[Path, Path, Path]:
    project_root = tmp_path / "project"
    state_dir = project_root / ".crewplane"
    cache_root = tmp_path / "workspace-cache"
    project_root.mkdir()
    if initialize_git:
        _git(project_root, "init")
        _git(project_root, "config", "user.name", "Crewplane Test")
        _git(project_root, "config", "user.email", "crewplane-test@example.invalid")
        (project_root / "README.md").write_text("ready\n", encoding="utf-8")
        _git(project_root, "add", ".")
        _git(project_root, "commit", "-m", "initial")
    repo_id = (
        cleanup_repository_id(project_root, all_projects=False)
        if initialize_git
        else "repo-1"
    )
    workspace_path = cache_root / "workspaces" / repo_id / "run-1" / "node-round1"
    if create_workspace:
        workspace_path.mkdir(parents=True)
        (workspace_path / "file.txt").write_text("payload", encoding="utf-8")
    state_dir.mkdir(parents=True)
    config_path = state_dir / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                f'version: "{SCHEMA_VERSION}"',
                "agents:",
                "  alpha:",
                '    cli_cmd: ["mock"]',
                '    default_model: "test"',
                "settings:",
                "  workspace:",
                "    enabled: true",
                f'    cache_root: "{cache_root.as_posix()}"',
                "    cleanup_on_success: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return project_root, config_path, workspace_path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")
