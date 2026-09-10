from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.service import MaterializationLimiter
from crewplane.runtime.workspace.state import WorkspaceStateRetention
from crewplane.runtime.workspace.state_evidence import record_workspace_process_drain
from crewplane.runtime.workspace.worktree import (
    cache as worktree_cache,
)
from crewplane.runtime.workspace.worktree.cache import (
    ReusableWorktreeCheckout,
    WorktreeReuseCache,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_output_manager,
    workspace_plan,
)
from tests.helpers.workspace_worktree_reuse import (
    two_node_lineage_plan,
    workspace_request,
)


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_fresh_worktree_terminal_failure_removes_checkout_when_cache_exists(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = two_node_lineage_plan(repo, cache_root)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    workspace = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert workspace.workspace_path is not None
    workspace_path = workspace.workspace_path
    assert workspace_path.exists()

    if terminal_status == "failed":
        workspace.mark_failed("provider failed")
    else:
        workspace.mark_cancelled("provider cancelled")

    state = read_json_object(
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    assert state["status"] == terminal_status
    assert state["workspace"]["retention"] == "deleted"
    assert state["workspace"]["retained_reason"] is None
    assert not workspace_path.exists()
    assert not reuse_cache.owns(workspace_path)


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
@pytest.mark.parametrize("unresolved_drain", [False, True])
def test_reused_worktree_terminal_failure_defers_shared_checkout_cleanup(
    tmp_path: Path,
    terminal_status: str,
    unresolved_drain: bool,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = two_node_lineage_plan(repo, cache_root)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    output.create_node_dir(node_artifact_request("verify"))
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    first_workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()
    assert first_workspace_path.exists()

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert second.workspace_path == first_workspace_path

    first_state_path = (
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    first_state_before = first_state_path.read_bytes()
    assert second.state_path is not None
    mark_terminal = (
        second.mark_failed if terminal_status == "failed" else second.mark_cancelled
    )
    if unresolved_drain:
        record_workspace_process_drain(second.state_path, "unresolved", 123, 123)
        mark_terminal("provider drain unresolved")
        fenced_cleanup = reuse_cache.cleanup_all()
        assert len(fenced_cleanup.errors) == 1
        assert "remains leased" in str(fenced_cleanup.errors[0])
        assert first_workspace_path.exists()
        assert first_state_path.read_bytes() == first_state_before
        assert read_json_object(second.state_path)["workspace"]["retention"] == (
            "retained"
        )
        record_workspace_process_drain(second.state_path, "confirmed", 123, 123)
    mark_terminal(f"provider {terminal_status}")

    state = read_json_object(
        output.create_node_dir(node_artifact_request("verify")) / "workspace-state.json"
    )
    first_state = read_json_object(first_state_path)
    assert state["status"] == terminal_status
    assert state["workspace"]["retention"] == "pending_cleanup"
    assert first_state["workspace"]["retention"] == "pending_cleanup"
    assert first_state_path.read_bytes() == first_state_before
    assert first_workspace_path.exists()
    assert reuse_cache.owns(first_workspace_path)
    first.cleanup_after_success()
    assert first_workspace_path.exists()
    cleanup = reuse_cache.cleanup_all()
    assert cleanup.errors == ()
    assert first_state_path in cleanup.updated_state_paths
    assert second.state_path in cleanup.updated_state_paths
    assert all(
        read_json_object(path)["workspace"]["retention"] == "deleted"
        for path in cleanup.updated_state_paths
    )
    assert not first_workspace_path.exists()
    assert not reuse_cache.owns(first_workspace_path)
    assert reuse_cache.cleanup_all().errors == ()


def test_reused_worktree_cleanup_retry_updates_current_generation_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = two_node_lineage_plan(repo, tmp_path / "cache")
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    output.create_node_dir(node_artifact_request("verify"))
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert second.workspace_path == workspace_path
    assert second.state_path is not None
    current_state_path = second.state_path
    prior_state_path = first.state_path
    assert prior_state_path is not None
    original_update = worktree_cache.update_workspace_retention
    update_count = 0

    def fail_first_update(
        state_path: Path,
        retention: WorkspaceStateRetention,
    ) -> None:
        nonlocal update_count
        update_count += 1
        if update_count == 1:
            raise OSError("injected state projection failure")
        original_update(state_path, retention)

    monkeypatch.setattr(
        worktree_cache,
        "update_workspace_retention",
        fail_first_update,
    )

    second.mark_failed("provider failed")

    assert workspace_path.exists()
    first_cleanup = reuse_cache.cleanup_all()
    assert len(first_cleanup.errors) == 1
    assert isinstance(first_cleanup.errors[0], OSError)
    assert not workspace_path.exists()
    current_state = read_json_object(current_state_path)
    current_workspace = current_state["workspace"]
    assert isinstance(current_workspace, dict)
    assert current_workspace["retention"] == "pending_cleanup"
    assert current_workspace["retained_reason"] == "failure"

    cleanup = reuse_cache.cleanup_all()

    assert cleanup.errors == ()
    assert cleanup.updated_state_paths == tuple(
        sorted((prior_state_path, current_state_path))
    )
    assert read_json_object(prior_state_path)["workspace"]["retention"] == "deleted"
    assert read_json_object(current_state_path)["workspace"]["retention"] == "deleted"


def test_reuse_cache_eviction_paths_do_not_rewrite_state(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    checkout_root = tmp_path / "workspace" / "checkout"
    checkout_root.mkdir(parents=True)
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text('{"status":"succeeded"}\n', encoding="utf-8")
    source_ref = WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=source.run_base_commit,
        source_tree=source.source_tree,
    )
    stale_ref = WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit="0" * 40,
        source_tree=source.source_tree,
    )
    cache = WorktreeReuseCache()
    cache.store(
        ReusableWorktreeCheckout(
            node_id="implement",
            logical_worktree_name="implementation",
            workspace_path=checkout_root.parent,
            checkout_root=checkout_root,
            cwd=checkout_root,
            git_dir=tmp_path / "gitdir",
            source_commit=source_ref.source_commit,
            source_tree=source_ref.source_tree,
            source=source,
            state_path=state_path,
            cleanup_on_success=False,
            repository_id=source.repository_id,
            run_key_name=plan.run_key_name,
        )
    )

    assert (
        cache.take(
            "implementation",
            stale_ref,
            source.repository_id,
            plan.run_key_name,
        )
        is None
    )

    replacement_root = tmp_path / "replacement" / "checkout"
    replacement_root.mkdir(parents=True)
    cache.store(
        ReusableWorktreeCheckout(
            node_id="implement",
            logical_worktree_name="implementation",
            workspace_path=checkout_root.parent,
            checkout_root=checkout_root,
            cwd=checkout_root,
            git_dir=tmp_path / "gitdir",
            source_commit=source_ref.source_commit,
            source_tree=source_ref.source_tree,
            source=source,
            state_path=state_path,
            cleanup_on_success=False,
            repository_id=source.repository_id,
            run_key_name=plan.run_key_name,
        )
    )
    cache.store(
        ReusableWorktreeCheckout(
            node_id="verify",
            logical_worktree_name="implementation",
            workspace_path=replacement_root.parent,
            checkout_root=replacement_root,
            cwd=replacement_root,
            git_dir=tmp_path / "replacement-gitdir",
            source_commit=source_ref.source_commit,
            source_tree=source_ref.source_tree,
            source=source,
            state_path=state_path,
            cleanup_on_success=False,
            repository_id=source.repository_id,
            run_key_name=plan.run_key_name,
        )
    )

    assert state_path.read_text(encoding="utf-8") == '{"status":"succeeded"}\n'
