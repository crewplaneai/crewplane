from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import crewplane.runtime.workspace.cleanup as workspace_cleanup
from crewplane.cli.app import app
from tests.unit.cli.cleanup_support import (
    cleanup_project,
    run_cleanup_git,
)


def test_cleanup_workspaces_defaults_to_advisory_dry_run(tmp_path: Path) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix()],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Would remove 1 workspace path(s)" in result.output
    assert workspace_path.exists()
    assert project_root.exists()


def test_cleanup_workspaces_yes_removes_paths(tmp_path: Path) -> None:
    _, config_path, workspace_path = cleanup_project(tmp_path, initialize_git=True)

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 1 workspace path(s)" in result.output
    assert not workspace_path.exists(), result.output


def test_cleanup_workspaces_rechecks_exact_worktree_identity_before_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    expected_git_dir = Path(state_payload["execution"]["worktree_git_dir"])
    original_remove = workspace_cleanup.remove_unknown_workspace_path
    replacement_marker = workspace_path / "checkout" / "replacement.txt"

    def replace_checkout_before_removal(
        path: Path,
        expected_common_git_dir: Path | None,
        expected_worktree_git_dir: Path | None,
    ) -> None:
        checkout_root = path / "checkout"
        run_cleanup_git(
            project_root,
            "worktree",
            "move",
            checkout_root.as_posix(),
            (tmp_path / "moved-original-checkout").as_posix(),
        )
        run_cleanup_git(
            project_root,
            "worktree",
            "add",
            "--detach",
            checkout_root.as_posix(),
            "HEAD",
        )
        replacement_marker.write_text("replacement", encoding="utf-8")
        replacement_git_dir = Path(
            run_cleanup_git(checkout_root, "rev-parse", "--git-dir").strip()
        ).resolve()
        assert replacement_git_dir != expected_git_dir
        original_remove(
            path,
            expected_common_git_dir,
            expected_worktree_git_dir,
        )

    monkeypatch.setattr(
        workspace_cleanup,
        "remove_unknown_workspace_path",
        replace_checkout_before_removal,
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
    )

    assert result.exit_code == 1
    assert "does not match persisted" in result.output
    assert "identity" in result.output
    assert replacement_marker.read_text(encoding="utf-8") == "replacement"
    retained = json.loads(state_path.read_text(encoding="utf-8"))
    assert retained["workspace"]["retention"] == "retained"


def test_cleanup_workspaces_retains_claim_without_planned_workspace_policy(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    plan_path = (
        project_root / ".crewplane/execution-stages/run-1/preflight/execution-plan.json"
    )
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_payload["nodes"][0]["workspace_policy"] = None
    plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Removed 0 workspace path(s)" in result.output
    assert "workspace evidence for the run is malformed" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_retains_claim_that_conflicts_with_plan_identity(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["workflow_signature"] = "different-workflow-signature"
    publication = payload["ref_publication"]
    publication["phase"] = "published"
    destinations = publication["destinations"]
    ref_targets = {
        destination["name"]: destination["target_oid"]
        for destination in destinations.values()
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    for ref_name, target_oid in ref_targets.items():
        run_cleanup_git(project_root, "update-ref", ref_name, target_oid)

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Removed 0 workspace path(s)" in result.output
    assert "workspace evidence for the run is malformed" in result.output
    assert workspace_path.exists()
    for ref_name, target_oid in ref_targets.items():
        assert (
            run_cleanup_git(project_root, "rev-parse", ref_name).strip() == target_oid
        )


def test_cleanup_workspaces_retains_reappeared_deleted_workspace(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["workspace"]["retention"] = "deleted"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    replacement_path = workspace_path / "checkout" / "replacement.txt"
    replacement_path.write_text("replacement", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Removed 0 workspace path(s)" in result.output
    assert "reappeared after deletion" in result.output
    assert replacement_path.read_text(encoding="utf-8") == "replacement"


def test_cleanup_workspaces_all_projects_allows_non_git_project(
    tmp_path: Path,
) -> None:
    _, config_path, workspace_path = cleanup_project(tmp_path)

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
    _, config_path, workspace_path = cleanup_project(tmp_path)

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
    _, config_path, workspace_path = cleanup_project(tmp_path)

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
    _, config_path, _ = cleanup_project(tmp_path)

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix()],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Use --all-projects" in " ".join(result.output.split())


def test_cleanup_workspaces_rejects_relative_cache_root(tmp_path: Path) -> None:
    _, config_path, workspace_path = cleanup_project(tmp_path, initialize_git=True)
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
    _, config_path, workspace_path = cleanup_project(
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
    project_root, config_path, workspace_path = cleanup_project(
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
