from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

import crewplane.cli.workspace_cleanup.eligibility as cleanup_eligibility
from crewplane.cli.app import app
from tests.helpers.resume import (
    make_run_manifest,
    write_run_manifest,
)
from tests.unit.cli.cleanup_support import (
    cleanup_project,
    resolve_workspace_cleanup_context,
    run_cleanup_git,
)


def test_cleanup_workspaces_yes_retains_active_run_assets(tmp_path: Path) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
        run_status="running",
    )
    run_cleanup_git(
        project_root, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD"
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 0 workspace path(s)" in result.output
    assert "retained: workspace state is running" in result.output
    assert workspace_path.exists()
    assert (
        run_cleanup_git(project_root, "for-each-ref", "refs/crewplane/runs/run-1") != ""
    )


def test_cleanup_workspaces_yes_allows_distinct_terminal_node_and_run_states(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
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
    run_cleanup_git(
        project_root, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD"
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 1 workspace path(s)" in result.output
    assert not workspace_path.exists()
    assert (
        run_cleanup_git(project_root, "for-each-ref", "refs/crewplane/runs/run-1") != ""
    )


def test_cleanup_workspaces_yes_preserves_unrecorded_run_refs(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    run_cleanup_git(
        project_root, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD"
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 1 workspace path(s)" in result.output
    assert not workspace_path.exists()
    assert (
        run_cleanup_git(project_root, "for-each-ref", "refs/crewplane/runs/run-1") != ""
    )


@pytest.mark.parametrize(
    "filter_args",
    [
        ["--failed"],
        ["--older-than", "1d"],
        ["--orphans"],
    ],
)
def test_cleanup_workspaces_filters_preserve_refs_for_unselected_state(
    tmp_path: Path,
    filter_args: list[str],
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    publication = state_payload["ref_publication"]
    publication["phase"] = "published"
    destinations = publication["destinations"]
    ref_targets = {
        destination["name"]: destination["target_oid"]
        for destination in destinations.values()
    }
    state_path.write_text(json.dumps(state_payload), encoding="utf-8")
    node_manifest_path = state_path.parents[1] / "manifests/nodes/node.json"
    node_manifest_path.parent.mkdir(parents=True)
    node_manifest_path.write_text("{}", encoding="utf-8")
    for ref_name, target_oid in ref_targets.items():
        run_cleanup_git(project_root, "update-ref", ref_name, target_oid)

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            *filter_args,
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Removed 0 workspace path(s)" in result.output
    assert workspace_path.exists()
    for ref_name, target_oid in ref_targets.items():
        assert (
            run_cleanup_git(project_root, "rev-parse", ref_name).strip() == target_oid
        )
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["ref_publication"]["phase"] == "published"


def test_cleanup_workspaces_orphans_removes_only_terminal_run_orphan(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    orphan_path = workspace_path.parent / "orphan-round1"
    orphan_path.mkdir()
    (orphan_path / "file.txt").write_text("orphan", encoding="utf-8")
    run_cleanup_git(
        project_root, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD"
    )

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
    assert (
        run_cleanup_git(project_root, "for-each-ref", "refs/crewplane/runs/run-1") != ""
    )


def test_cleanup_workspaces_orphans_retains_unverifiable_run(tmp_path: Path) -> None:
    _, config_path, orphan_path = cleanup_project(
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


@pytest.mark.parametrize("activity", ["live", "unverifiable", "read-error"])
def test_cleanup_eligibility_blocks_active_or_unreadable_run_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, activity: str
) -> None:
    _, config, workspace = cleanup_project(tmp_path, initialize_git=True)
    context = resolve_workspace_cleanup_context(config)
    lock = Mock(return_value=activity)
    if activity == "read-error":
        lock.side_effect = OSError("lock unavailable")
    monkeypatch.setattr(cleanup_eligibility, "run_lock_activity", lock)
    lookup = cleanup_eligibility.workspace_cleanup_eligibility_lookup(context)
    assert lookup is not None

    result = lookup("run-1", workspace, "succeeded")

    assert not result.deletable
    assert (
        result.reason
        == f"run lock is {'live' if activity == 'live' else 'unverifiable'}"
    )
    assert workspace.exists()


@pytest.mark.parametrize("message", ["provider still running", ""])
def test_cleanup_eligibility_blocks_unverifiable_provider_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    _, config, workspace = cleanup_project(tmp_path, initialize_git=True)
    context = resolve_workspace_cleanup_context(config)
    monkeypatch.setattr(
        cleanup_eligibility, "run_lock_activity", Mock(return_value="none")
    )
    monkeypatch.setattr(
        cleanup_eligibility,
        "ensure_no_live_provider_processes",
        Mock(side_effect=RuntimeError(message)),
    )
    lookup = cleanup_eligibility.workspace_cleanup_eligibility_lookup(context)
    assert lookup is not None

    result = lookup("run-1", workspace, "succeeded")

    assert not result.deletable
    assert result.reason == (message or "provider process state is unverifiable")
    assert workspace.exists()


@pytest.mark.parametrize("status", [None, "running", "invalid"])
def test_cleanup_eligibility_rejects_unverified_workspace_state(
    tmp_path: Path, status: str | None
) -> None:
    _, config, workspace = cleanup_project(tmp_path, initialize_git=True)
    context = resolve_workspace_cleanup_context(config)
    lookup = cleanup_eligibility.workspace_cleanup_eligibility_lookup(context)
    assert lookup is not None

    result = lookup("run-1", workspace, status)

    assert not result.deletable
    assert result.reason == (
        "workspace state is unverifiable"
        if status is None
        else f"workspace state is {status}"
    )
