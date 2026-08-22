from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Never

import pytest
from typer.testing import CliRunner

import crewplane.cli.cleanup as cleanup_cli
from crewplane.cli.app import app
from crewplane.cli.cleanup import cleanup_repository_id, load_workspace_statuses
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_run_manifest, write_run_manifest


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


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("read-only cache"),
        OSError("workspace I/O failure"),
    ],
)
def test_cleanup_workspaces_reports_mutation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
) -> None:
    _, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )

    def fail_cleanup(
        context: object,
        destructive: bool,
    ) -> Never:
        del context, destructive
        raise failure

    monkeypatch.setattr(cleanup_cli, "execute_workspace_cleanup", fail_cleanup)

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
    )

    assert result.exit_code == 1
    assert f"Cleanup failed: {failure}" in result.output
    assert not isinstance(result.exception, OSError)
    assert workspace_path.exists()


def test_cleanup_workspaces_yes_retains_active_run_assets(tmp_path: Path) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
        run_status="running",
    )
    _git(project_root, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 0 workspace path(s)" in result.output
    assert "retained: workspace state is running" in result.output
    assert workspace_path.exists()
    assert _git(project_root, "for-each-ref", "refs/crewplane/runs/run-1") != ""


def test_cleanup_workspaces_yes_allows_distinct_terminal_node_and_run_states(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    write_run_manifest(
        project_root / ".crewplane",
        make_run_manifest(
            run_id="run-1",
            run_key_name="run-1",
            status="failed",
        ),
    )
    _git(project_root, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 1 workspace path(s)" in result.output
    assert not workspace_path.exists()
    assert _git(project_root, "for-each-ref", "refs/crewplane/runs/run-1") == ""


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


def test_cleanup_workspaces_orphans_removes_only_terminal_run_orphan(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    orphan_path = workspace_path.parent / "orphan-round1"
    orphan_path.mkdir()
    (orphan_path / "file.txt").write_text("orphan", encoding="utf-8")
    _git(project_root, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD")

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            "--orphans",
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 1 workspace path(s)" in result.output
    assert workspace_path.exists()
    assert not orphan_path.exists()
    assert _git(project_root, "for-each-ref", "refs/crewplane/runs/run-1") != ""


def test_cleanup_workspaces_orphans_retains_unverifiable_run(tmp_path: Path) -> None:
    _, config_path, orphan_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
        create_workspace=False,
    )
    orphan_path.mkdir(parents=True)
    (orphan_path / "file.txt").write_text("orphan", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            "--orphans",
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 0 workspace path(s)" in result.output
    assert "run manifest is missing or unsafe" in result.output
    assert orphan_path.exists()


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
    assert "cannot verify cross-project run ownership or activity" in result.output
    assert "Would remove 1 workspace path(s)" in result.output
    assert "status=unknown" in result.output
    assert "status=orphan" not in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_all_projects_yes_is_explicit_global_override(
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
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "cannot verify cross-project run ownership or activity" in result.output
    assert "Removed 1 workspace path(s)" in result.output
    assert not workspace_path.exists()


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


def test_cleanup_workspaces_rejects_dangling_cache_root_symlink(
    tmp_path: Path,
) -> None:
    _, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
        create_workspace=False,
    )
    cache_root = workspace_path.parents[3]
    try:
        cache_root.symlink_to(
            tmp_path / "missing-cache-target", target_is_directory=True
        )
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Workspace cache root must not be a symlink" in result.output
    assert cache_root.is_symlink()


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


def test_load_workspace_statuses_skips_twice_hydrated_state_without_cache_key(
    tmp_path: Path,
) -> None:
    stage_root = tmp_path / "execution-stages"
    state_path = stage_root / "workflow--resumed" / "node" / "workspace-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "run_key_name": "workflow--resumed",
                "status": "succeeded",
                "workspace": {
                    "cache_key": None,
                    "retention": "not_applicable",
                    "retained_reason": "hydrated_resume",
                },
                "resume_origin": {
                    "source_run_id": "first-resume",
                    "source_run_key_name": "workflow--first-resume",
                    "source_node_id": "node",
                    "source_workspace": {
                        "cache_key": None,
                        "retention": "not_applicable",
                        "retained_reason": "hydrated_resume",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    assert load_workspace_statuses(stage_root) == {}


def _cleanup_project(
    tmp_path: Path,
    initialize_git: bool = False,
    create_workspace: bool = True,
    run_status: str = "succeeded",
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
    if create_workspace:
        state_path = (
            state_dir / "execution-stages" / "run-1" / "node" / "workspace-state.json"
        )
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "run_key_name": "run-1",
                    "status": run_status,
                    "workspace": {"cache_key": workspace_path.name},
                }
            ),
            encoding="utf-8",
        )
        write_run_manifest(
            state_dir,
            make_run_manifest(
                run_id="run-1",
                run_key_name="run-1",
                status=run_status,
            ),
        )
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
