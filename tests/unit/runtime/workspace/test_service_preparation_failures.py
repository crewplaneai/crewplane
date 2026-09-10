from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import crewplane.runtime.workspace.service.common as workspace_service_common
import crewplane.runtime.workspace.service.snapshot as workspace_service_snapshot
import crewplane.runtime.workspace.service.worktree as workspace_service_worktree
from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.invocation import invocation_slug
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)


def test_snapshot_workspace_failure_removes_disposable_checkout(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    prepared.mark_failed("provider failed")

    failed_state = read_json_object(prepared.state_path)
    assert failed_state["status"] == "failed"
    assert failed_state["workspace"]["retention"] == "deleted"
    assert failed_state["workspace"]["retained_reason"] is None
    assert not prepared.workspace_path.exists()


def test_snapshot_workspace_cancellation_removes_disposable_checkout(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    prepared.mark_cancelled("provider cancelled")

    cancelled_state = read_json_object(prepared.state_path)
    assert cancelled_state["status"] == "cancelled"
    assert cancelled_state["workspace"]["retention"] == "deleted"
    assert cancelled_state["workspace"]["retained_reason"] is None
    assert not prepared.workspace_path.exists()


def test_snapshot_workspace_preparation_failure_removes_workspace_path(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False)
    assert plan.workspace_source is not None
    bad_source = plan.workspace_source.model_copy(update={"run_base_commit": "f" * 40})
    plan = plan.model_copy(update={"workspace_source": bad_source})
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    with pytest.raises(subprocess.CalledProcessError):
        prepare_invocation_workspace(
            workspace_invocation_request(plan, output),
            workspace_invocation_context(),
        )

    state_path = (
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    failed_state = read_json_object(state_path)
    assert failed_state["status"] == "failed"
    assert failed_state["workspace"]["materialization"] == "snapshot_checkout"
    assert failed_state["workspace"]["retention"] == "deleted"
    assert "result" not in failed_state
    source = plan.workspace_source
    assert source is not None
    workspace_path = (
        cache_root
        / "snapshots"
        / source.repository_id
        / "workspace-run-001"
        / invocation_slug("implement", "alpha", None, 1)
    )
    assert not workspace_path.exists()


@pytest.mark.parametrize("kind", ["snapshot", "worktree"])
@pytest.mark.parametrize("collision_kind", ["directory", "dangling_symlink"])
def test_workspace_collision_is_retained_without_removal(
    tmp_path: Path,
    collision_kind: str,
    kind: str,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False, kind=kind)
    output = workspace_output_manager(tmp_path, repo)
    stage_dir = output.create_node_dir(node_artifact_request("implement"))
    source = plan.workspace_source
    assert source is not None
    workspace_path = (
        cache_root
        / ("snapshots" if kind == "snapshot" else "workspaces")
        / source.repository_id
        / plan.run_key_name
        / invocation_slug("implement", "alpha", None, 1)
    )
    workspace_path.parent.mkdir(parents=True)
    sentinel = workspace_path / "must-survive.txt"
    if collision_kind == "directory":
        workspace_path.mkdir()
        sentinel.write_text("unowned data\n", encoding="utf-8")
    else:
        try:
            workspace_path.symlink_to(tmp_path / "missing", target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation is unavailable")

    with pytest.raises(RuntimeError, match="already exists"):
        prepare_invocation_workspace(
            workspace_invocation_request(plan, output),
            workspace_invocation_context(),
        )

    failed_state = read_json_object(stage_dir / "workspace-state.json")
    assert failed_state["status"] == "failed"
    assert failed_state["workspace"]["retention"] == "retained"
    assert workspace_path.exists() or workspace_path.is_symlink()
    if collision_kind == "directory":
        assert sentinel.read_text(encoding="utf-8") == "unowned data\n"


def test_snapshot_capacity_rejection_writes_failed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False)
    runtime_snapshot = dict(plan.runtime_config_snapshot)
    workspace = dict(runtime_snapshot["workspace"])
    workspace["disk"] = {"fail_free_bytes": 1}
    runtime_snapshot["workspace"] = workspace
    plan = plan.model_copy(update={"runtime_config_snapshot": runtime_snapshot})
    output = workspace_output_manager(tmp_path, repo)
    stage_dir = output.create_node_dir(node_artifact_request("implement"))

    def exhausted_disk(path: Path) -> SimpleNamespace:
        del path
        return SimpleNamespace(free=0)

    monkeypatch.setattr(
        "crewplane.runtime.workspace.materialization.shutil.disk_usage",
        exhausted_disk,
    )

    with pytest.raises(RuntimeError, match="fail_free_bytes"):
        prepare_invocation_workspace(
            workspace_invocation_request(plan, output),
            workspace_invocation_context(),
        )

    failed_state = read_json_object(stage_dir / "workspace-state.json")
    assert failed_state["status"] == "failed"
    assert failed_state["workspace"]["retention"] == "deleted"
    assert not cache_root.exists()


def test_worktree_workspace_preparation_failure_writes_failed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False, kind="worktree")
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    def fail_materialize_worktree_workspace(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("worktree materialization boom")

    monkeypatch.setattr(
        workspace_service_worktree,
        "materialize_worktree_workspace",
        fail_materialize_worktree_workspace,
    )

    with pytest.raises(RuntimeError, match="worktree materialization boom"):
        prepare_invocation_workspace(
            workspace_invocation_request(plan, output),
            workspace_invocation_context(),
        )

    state_path = (
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    failed_state = read_json_object(state_path)
    assert failed_state["status"] == "failed"
    assert failed_state["workspace"]["materialization"] == "worktree_checkout"
    assert failed_state["workspace"]["retention"] == "deleted"
    assert failed_state["workspace"]["retained_reason"] is None
    assert failed_state["workspace"]["lineage_producer"] is True
    assert "result" not in failed_state


def test_snapshot_workspace_preparation_cleanup_failure_notes_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    def fail_materialize_snapshot(
        source: object,
        checkout_root: Path,
        index_path: Path,
    ) -> None:
        del source, checkout_root, index_path
        raise RuntimeError("materialize boom")

    def fail_remove_workspace_path(path: Path) -> None:
        del path
        raise RuntimeError("cleanup boom")

    monkeypatch.setattr(
        workspace_service_snapshot,
        "materialize_snapshot",
        fail_materialize_snapshot,
    )
    monkeypatch.setattr(
        workspace_service_common,
        "remove_workspace_path",
        fail_remove_workspace_path,
    )

    with pytest.raises(RuntimeError, match="materialize boom") as exc_info:
        prepare_invocation_workspace(
            workspace_invocation_request(plan, output),
            workspace_invocation_context(),
        )

    assert _exception_notes_contain(
        exc_info.value,
        "Workspace cleanup after preparation failure failed: cleanup boom",
    )
    shutil.rmtree(cache_root, ignore_errors=True)


def _exception_notes_contain(exc: BaseException, expected: str) -> bool:
    return any(expected in note for note in getattr(exc, "__notes__", ()))


@pytest.mark.parametrize("kind", ["snapshot", "worktree"])
def test_unmaterialized_state_write_failure_preserves_primary_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=False, kind=kind)
    output = workspace_output_manager(tmp_path, repo)
    stage_dir = output.create_node_dir(node_artifact_request("implement"))
    failure = RuntimeError("preparation stopped")

    def fail_preparation(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise failure

    def fail_state_write(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("write denied")

    module, operation = (
        (workspace_service_snapshot, "create_snapshot_workspace")
        if kind == "snapshot"
        else (workspace_service_worktree, "materialize_worktree_workspace")
    )
    monkeypatch.setattr(module, operation, fail_preparation)
    monkeypatch.setattr(
        workspace_service_common, "update_workspace_state", fail_state_write
    )
    with pytest.raises(RuntimeError, match="preparation stopped") as exc_info:
        prepare_invocation_workspace(
            workspace_invocation_request(plan, output), workspace_invocation_context()
        )
    assert exc_info.value is failure
    assert failure.__notes__ == [
        "Workspace failure-state recording after preparation failure failed: write denied"
    ] * (2 if kind == "snapshot" else 1)
    assert read_json_object(stage_dir / "workspace-state.json")["status"] == "running"
