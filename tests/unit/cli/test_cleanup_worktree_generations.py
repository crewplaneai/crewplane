from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from typer.testing import CliRunner

import crewplane.runtime.workspace.cleanup as workspace_cleanup
from crewplane.cli.app import app
from crewplane.runtime.workspace.state import WorkspaceStateRetention
from tests.helpers.resume import (
    make_run_manifest,
    write_run_manifest,
)
from tests.unit.cli.cleanup_support import (
    cleanup_project,
    run_cleanup_git,
)


def test_cleanup_workspaces_removes_one_coherent_multi_generation_worktree(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    generation_one = json.loads(state_path.read_text(encoding="utf-8"))
    generation_two = deepcopy(generation_one)
    generation_two["workspace"]["reuse_generation"] = 2
    generation_two_path = state_path.with_name(
        "workspace-reuse-claim-node-generation-2.json"
    )
    generation_two_path.write_text(json.dumps(generation_two), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 1 workspace path(s)" in result.output
    assert not workspace_path.exists()
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["workspace"]["retention"]
        == "deleted"
    )
    assert (
        json.loads(generation_two_path.read_text(encoding="utf-8"))["workspace"][
            "retention"
        ]
        == "deleted"
    )


def test_cleanup_workspaces_reconciles_generation_states_after_partial_write(
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
    generation_one = json.loads(state_path.read_text(encoding="utf-8"))
    generation_one["status"] = "failed"
    generation_one["workspace"]["retention"] = "pending_cleanup"
    state_path.write_text(json.dumps(generation_one), encoding="utf-8")
    generation_two = deepcopy(generation_one)
    generation_two["status"] = "cancelled"
    generation_two["workspace"]["reuse_generation"] = 2
    generation_two_path = state_path.with_name(
        "workspace-reuse-claim-node-generation-2.json"
    )
    generation_two_path.write_text(json.dumps(generation_two), encoding="utf-8")
    original_update = workspace_cleanup.update_workspace_retention
    update_count = 0

    def fail_second_state_update(
        state_path_arg: Path,
        retention: WorkspaceStateRetention,
    ) -> None:
        nonlocal update_count
        update_count += 1
        if update_count == 2:
            raise OSError("injected second-state write failure")
        original_update(state_path_arg, retention)

    monkeypatch.setattr(
        workspace_cleanup,
        "update_workspace_retention",
        fail_second_state_update,
    )

    first_result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
    )

    assert first_result.exit_code == 1
    assert not workspace_path.exists()
    first_payload = json.loads(state_path.read_text(encoding="utf-8"))
    second_payload = json.loads(generation_two_path.read_text(encoding="utf-8"))
    assert {
        first_payload["workspace"]["retention"],
        second_payload["workspace"]["retention"],
    } == {"pending_cleanup", "deleted"}

    monkeypatch.setattr(
        workspace_cleanup,
        "update_workspace_retention",
        original_update,
    )
    retry_result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert retry_result.exit_code == 0, retry_result.output
    assert "Removed 0 workspace path(s)" in retry_result.output
    first_payload = json.loads(state_path.read_text(encoding="utf-8"))
    second_payload = json.loads(generation_two_path.read_text(encoding="utf-8"))
    assert first_payload["status"] == "failed"
    assert second_payload["status"] == "cancelled"
    assert first_payload["workspace"]["retention"] == "deleted"
    assert second_payload["workspace"]["retention"] == "deleted"


@pytest.mark.parametrize("retention", ["pending_cleanup", "deleted"])
def test_cleanup_workspaces_failed_filter_reconciles_absent_workspace_refs(
    tmp_path: Path,
    retention: str,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
        run_status="failed",
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["workspace"]["retention"] = retention
    payload["ref_publication"]["phase"] = "published"
    destinations = payload["ref_publication"]["destinations"]
    ref_targets = {
        destination["name"]: destination["target_oid"]
        for destination in destinations.values()
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    for ref_name, target_oid in ref_targets.items():
        run_cleanup_git(project_root, "update-ref", ref_name, target_oid)
    run_cleanup_git(
        project_root,
        "worktree",
        "remove",
        "--force",
        (workspace_path / "checkout").as_posix(),
    )
    workspace_path.rmdir()

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            "--failed",
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Removed 0 workspace path(s)" in result.output
    for ref_name in ref_targets:
        assert run_cleanup_git(project_root, "for-each-ref", ref_name).strip() == ""
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["workspace"]["retention"] == "deleted"
    assert persisted["ref_publication"]["phase"] == "removed"


def test_cleanup_workspaces_does_not_reconcile_absent_state_for_active_run(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_dir = project_root / ".crewplane"
    state_path = state_dir / "execution-stages/run-1/node/workspace-state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["workspace"]["retention"] = "pending_cleanup"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    write_run_manifest(
        state_dir,
        make_run_manifest(
            run_id="run-1",
            run_key_name="run-1",
            status="running",
        ),
    )
    run_cleanup_git(
        project_root,
        "worktree",
        "remove",
        "--force",
        (workspace_path / "checkout").as_posix(),
    )
    workspace_path.rmdir()

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 0 workspace path(s)" in result.output
    retained = json.loads(state_path.read_text(encoding="utf-8"))
    assert retained["workspace"]["retention"] == "pending_cleanup"


def test_cleanup_workspaces_retains_duplicate_generation_claims(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    duplicate_path = state_path.with_name("workspace-reuse-claim-duplicate.json")
    duplicate_path.write_bytes(state_path.read_bytes())

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "duplicate generation claims" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_orphans_retains_corrupt_workspace_claim(
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
    payload["execution"].pop("workspace_path")
    state_path.write_text(json.dumps(payload), encoding="utf-8")

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

    assert result.exit_code == 0, result.output
    assert "evidence for the run is malformed" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_orphans_retains_claim_omitted_from_plan(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    plan_path = (
        project_root / ".crewplane/execution-stages/run-1/preflight/execution-plan.json"
    )
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["nodes"] = []
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

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

    assert result.exit_code == 0, result.output
    assert "Removed 0 workspace path(s)" in result.output
    assert "evidence for the run is malformed" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_retains_worktree_with_wrong_git_backlink(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    git_dir = Path(
        run_cleanup_git(workspace_path / "checkout", "rev-parse", "--git-dir").strip()
    )
    (git_dir / "gitdir").write_text(
        (tmp_path / "other" / ".git").as_posix(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "identity or disposal safety is unverifiable" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_yes_ignores_symlink_workspace_candidates(
    tmp_path: Path,
) -> None:
    _, config_path, workspace_path = cleanup_project(
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
