from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.service import MaterializationLimiter
from crewplane.runtime.workspace.worktree import (
    materialization as worktree_materialization,
)
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from crewplane.runtime.workspace.worktree import reset as worktree_reset
from crewplane.runtime.workspace.worktree import reuse as worktree_reuse
from crewplane.runtime.workspace.worktree.cache import (
    ReusableWorktreeCheckout,
    WorktreeReuseCache,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.resume import make_workspace_source_snapshot
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    run_git_text,
    workspace_invocation_context,
    workspace_output_manager,
    workspace_plan,
)
from tests.helpers.workspace_worktree_reuse import (
    two_node_lineage_plan,
    workspace_request,
)


def test_same_worktree_reuses_checkout_with_incremental_reset(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    nested = repo / "packages" / "app"
    nested.mkdir(parents=True)
    (nested / ".gitignore").write_text(".cache/\n", encoding="utf-8")
    run_git_text(repo, "add", "packages/app/.gitignore")
    run_git_text(repo, "commit", "-m", "ignore caches")
    cache_root = tmp_path / "cache"
    plan = two_node_lineage_plan(repo, cache_root)
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")
    output.create_stage_dir("verify")
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    first_workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    ignored_cache = first.cwd / "packages" / "app" / ".cache" / "tool"
    ignored_cache.mkdir(parents=True)
    (ignored_cache / "leftover.txt").write_text("ignored\n", encoding="utf-8")

    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()
    assert first_workspace_path.exists()

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )

    try:
        assert second.workspace_path == first_workspace_path
        assert (second.cwd / "result.txt").read_text(encoding="utf-8") == "captured\n"
        assert not (second.cwd / "packages" / "app" / ".cache").exists()
        assert run_git_text(second.cwd, "status", "--porcelain=v1") == ""
        first_state = read_json_object(
            output.create_stage_dir("implement") / "workspace-state.json"
        )
        second_state = read_json_object(
            output.create_stage_dir("verify") / "workspace-state.json"
        )
        assert first_state["workspace"]["retention"] == "pending_cleanup"
        assert second_state["reuse"]["strategy"] == "incremental_reset"
        assert second_state["reuse"]["reused"] is True
        assert (
            second_state["reuse"]["previous_workspace_state"] == "workspace-state.json"
        )
        assert second_state["git"]["worktree_lock_mode"] == "reused_incremental_reset"
    finally:
        if second.workspace_path is not None:
            second.mark_succeeded(defer_cleanup=True)
        reuse_cache.cleanup_all_best_effort()


def test_retained_successful_worktree_is_not_reused(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = two_node_lineage_plan(repo, cache_root, cleanup_on_success=False)
    source = plan.workspace_source
    assert source is not None
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")
    output.create_stage_dir("verify")
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    first_workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded()

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )

    try:
        assert second.workspace_path is not None
        assert second.workspace_path != first_workspace_path
        assert first_workspace_path.exists()
        first_state = read_json_object(
            output.create_stage_dir("implement") / "workspace-state.json"
        )
        second_state = read_json_object(
            output.create_stage_dir("verify") / "workspace-state.json"
        )
        assert first_state["workspace"]["retention"] == "retained"
        assert first_state["workspace"]["retained_reason"] == "cleanup_on_success_false"
        assert second_state["reuse"]["strategy"] == "fresh_checkout"
        assert second_state["reuse"]["reused"] is False
    finally:
        if second.workspace_path is not None:
            remove_worktree_workspace(source, second.workspace_path)
        if first_workspace_path.exists():
            remove_worktree_workspace(source, first_workspace_path)


def test_reuse_cache_node_cleanup_updates_all_reused_state_paths(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = two_node_lineage_plan(repo, cache_root)
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")
    output.create_stage_dir("verify")
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
    first.cleanup_after_success()

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert second.workspace_path == workspace_path
    second.mark_succeeded(defer_cleanup=True)
    second.cleanup_after_success()

    errors = reuse_cache.cleanup_node_best_effort("verify")

    assert errors == ()
    assert not workspace_path.exists()
    first_state = read_json_object(
        output.create_stage_dir("implement") / "workspace-state.json"
    )
    second_state = read_json_object(
        output.create_stage_dir("verify") / "workspace-state.json"
    )
    assert first_state["workspace"]["retention"] == "deleted"
    assert second_state["workspace"]["retention"] == "deleted"


def test_reuse_cache_cleanup_all_removes_leased_checkout(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = two_node_lineage_plan(repo, cache_root)
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")
    output.create_stage_dir("verify")
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
    first.cleanup_after_success()

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert second.workspace_path == workspace_path

    cleanup = reuse_cache.cleanup_all()

    assert cleanup.errors == ()
    assert output.create_stage_dir("implement") / "workspace-state.json" in (
        cleanup.updated_state_paths
    )
    assert not workspace_path.exists()
    first_state = read_json_object(
        output.create_stage_dir("implement") / "workspace-state.json"
    )
    assert first_state["workspace"]["retention"] == "deleted"


def test_reuse_cache_retries_stale_entry_cleanup_after_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reuse_cache = WorktreeReuseCache()
    workspace_path = tmp_path / "workspace"
    checkout_root = workspace_path / "checkout"
    checkout_root.mkdir(parents=True)
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "succeeded",
                "workspace": {
                    "retention": "pending_cleanup",
                    "retained_reason": "stage_finalization_pending",
                },
            }
        ),
        encoding="utf-8",
    )
    entry = ReusableWorktreeCheckout(
        node_id="implement",
        logical_worktree_name="primary",
        workspace_path=workspace_path,
        checkout_root=checkout_root,
        cwd=checkout_root,
        git_dir=workspace_path / "git-dir",
        source_commit="old-commit",
        source_tree="old-tree",
        source=make_workspace_source_snapshot(),
        state_path=state_path,
        cleanup_on_success=True,
    )
    reuse_cache.store(entry)
    removal_attempts = 0

    def remove_or_fail(source: object, path: Path) -> None:
        nonlocal removal_attempts
        del source
        removal_attempts += 1
        if removal_attempts == 1:
            raise RuntimeError("cleanup failed")
        shutil.rmtree(path)

    monkeypatch.setattr(
        "crewplane.runtime.workspace.worktree.cache.remove_worktree_workspace",
        remove_or_fail,
    )

    reused = reuse_cache.take(
        "primary",
        WorktreeSourceRef(
            source_kind="project",
            source_node_id=None,
            source_commit="new-commit",
            source_tree="new-tree",
        ),
    )

    assert reused is None
    assert reuse_cache.owns(workspace_path)
    assert read_json_object(state_path)["workspace"]["retention"] == "pending_cleanup"

    cleanup = reuse_cache.cleanup_all()

    assert cleanup.errors == ()
    assert cleanup.updated_state_paths == (state_path,)
    assert removal_attempts == 2
    assert not workspace_path.exists()
    assert read_json_object(state_path)["workspace"]["retention"] == "deleted"


def test_same_worktree_reuse_failure_falls_back_to_fresh_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = two_node_lineage_plan(repo, cache_root)
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")
    output.create_stage_dir("verify")
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

    def fail_reuse(
        workspace_path: Path,
        source: object,
        source_ref: object,
        expected_git_dir: object,
        protected_ref_scopes: object,
    ) -> None:
        assert workspace_path.exists()
        assert source is not None
        assert source_ref is not None
        assert expected_git_dir is not None
        assert protected_ref_scopes is not None
        raise RuntimeError("reset verification failed")

    monkeypatch.setattr(
        worktree_materialization,
        "reuse_worktree_workspace",
        fail_reuse,
    )

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )

    try:
        assert second.workspace_path is not None
        assert second.workspace_path != first_workspace_path
        state = read_json_object(
            output.create_stage_dir("verify") / "workspace-state.json"
        )
        assert state["reuse"]["strategy"] == "fresh_checkout"
        assert state["reuse"]["fallback"] is True
        assert "reset verification failed" in state["reuse"]["fallback_reason"]
    finally:
        if second.workspace_path is not None:
            second.mark_succeeded(defer_cleanup=True)
        reuse_cache.cleanup_all_best_effort()


def test_same_worktree_reuse_rejects_retargeted_git_file_before_reset(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = two_node_lineage_plan(repo, cache_root)
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")
    output.create_stage_dir("verify")
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    first_workspace_path = first.workspace_path
    assert first.worktree_capture is not None
    expected_git_dir = first.worktree_capture.git_dir
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()
    assert first_workspace_path.exists()

    git_file = first_workspace_path / "checkout" / ".git"
    git_file.write_text(f"gitdir: {(repo / '.git').as_posix()}\n", encoding="utf-8")

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )

    try:
        assert second.workspace_path is not None
        assert second.workspace_path != first_workspace_path
        state = read_json_object(
            output.create_stage_dir("verify") / "workspace-state.json"
        )
        assert state["reuse"]["strategy"] == "fresh_checkout"
        assert state["reuse"]["fallback"] is True
        assert ".git file does not match Git dir" in state["reuse"]["fallback_reason"]
        assert expected_git_dir != repo / ".git"
    finally:
        if first_workspace_path.exists():
            git_file.write_text(f"gitdir: {expected_git_dir.as_posix()}\n")
        if second.workspace_path is not None:
            second.mark_succeeded(defer_cleanup=True)
        reuse_cache.cleanup_all_best_effort()


def test_reusable_reset_rejects_symlinked_git_metadata_before_git_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    common_git_dir = repo_root / ".git"
    worktrees_dir = common_git_dir / "worktrees"
    checkout_root = tmp_path / "workspace" / "checkout"
    real_admin_dir = common_git_dir / "real-admin"
    checkout_root.mkdir(parents=True)
    worktrees_dir.mkdir(parents=True)
    real_admin_dir.mkdir()
    (real_admin_dir / "gitdir").write_text(
        (checkout_root / ".git").as_posix(),
        encoding="utf-8",
    )
    expected_git_dir = worktrees_dir / "checkout"
    expected_git_dir.symlink_to(real_admin_dir, target_is_directory=True)
    (checkout_root / ".git").write_text(
        f"gitdir: {expected_git_dir.as_posix()}\n",
        encoding="utf-8",
    )

    def ignore_common_git_policy_drift(repo_root: Path, common_git_dir: Path) -> None:
        del repo_root, common_git_dir

    monkeypatch.setattr(
        worktree_reset,
        "reject_common_git_policy_drift",
        ignore_common_git_policy_drift,
    )

    with pytest.raises(RuntimeError, match="metadata must not be symlinked"):
        worktree_reset.reset_reusable_worktree_checkout(
            checkout_root,
            "a" * 40,
            repo_root,
            common_git_dir,
            expected_git_dir,
        )


def test_reusable_reset_rejects_symlinked_git_info_before_unlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    common_git_dir = repo_root / ".git"
    expected_git_dir = common_git_dir / "worktrees" / "checkout"
    checkout_root = tmp_path / "workspace" / "checkout"
    outside_info = tmp_path / "outside-info"
    checkout_root.mkdir(parents=True)
    expected_git_dir.mkdir(parents=True)
    outside_info.mkdir()
    (expected_git_dir / "gitdir").write_text(
        (checkout_root / ".git").as_posix(),
        encoding="utf-8",
    )
    (checkout_root / ".git").write_text(
        f"gitdir: {expected_git_dir.as_posix()}\n",
        encoding="utf-8",
    )
    try:
        (expected_git_dir / "info").symlink_to(
            outside_info,
            target_is_directory=True,
        )
    except OSError:
        pytest.skip("symlink creation is unavailable")

    def ignore_common_git_policy_drift(repo_root: Path, common_git_dir: Path) -> None:
        del repo_root, common_git_dir

    monkeypatch.setattr(
        worktree_reset,
        "reject_common_git_policy_drift",
        ignore_common_git_policy_drift,
    )

    with pytest.raises(RuntimeError, match="Git info dir must be a real directory"):
        worktree_reset.reset_reusable_worktree_checkout(
            checkout_root,
            "a" * 40,
            repo_root,
            common_git_dir,
            expected_git_dir,
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
    output.create_stage_dir("implement")
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
        output.create_stage_dir("implement") / "workspace-state.json"
    )
    assert state["status"] == terminal_status
    assert state["workspace"]["retention"] == "deleted"
    assert state["workspace"]["retained_reason"] is None
    assert not workspace_path.exists()
    assert not reuse_cache.owns(workspace_path)


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_reused_worktree_terminal_failure_removes_active_checkout(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = two_node_lineage_plan(repo, cache_root)
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")
    output.create_stage_dir("verify")
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

    first_state_path = output.create_stage_dir("implement") / "workspace-state.json"
    if terminal_status == "failed":
        second.mark_failed("provider failed")
    else:
        second.mark_cancelled("provider cancelled")
    first.cleanup_after_success()

    state = read_json_object(output.create_stage_dir("verify") / "workspace-state.json")
    first_state = read_json_object(first_state_path)
    assert state["status"] == terminal_status
    assert state["workspace"]["retention"] == "deleted"
    assert state["workspace"]["retained_reason"] is None
    assert first_state["workspace"]["retention"] == "deleted"
    assert not first_workspace_path.exists()
    assert not reuse_cache.owns(first_workspace_path)
    cleanup = reuse_cache.cleanup_all()
    assert cleanup.errors == ()
    assert first_state_path in cleanup.updated_state_paths
    assert not first_workspace_path.exists()


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
        )
    )

    assert cache.take("implementation", stale_ref) is None

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
        )
    )

    assert state_path.read_text(encoding="utf-8") == '{"status":"succeeded"}\n'


def test_reused_worktree_rejects_source_tree_mismatch_before_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
    wrong_tree = "f" * 40
    assert wrong_tree != source.source_tree
    reset_called = False

    def reject_reset(
        checkout_root: Path,
        source_commit: str,
        repo_root: Path,
        common_git_dir: Path,
        expected_git_dir: Path,
    ) -> None:
        nonlocal reset_called
        del checkout_root, source_commit, repo_root, common_git_dir, expected_git_dir
        reset_called = True
        raise AssertionError("reset should not run for mismatched source trees")

    monkeypatch.setattr(
        worktree_reuse,
        "reset_reusable_worktree_checkout",
        reject_reset,
    )

    with pytest.raises(RuntimeError, match="source tree mismatch"):
        worktree_reuse.reuse_worktree_workspace(
            tmp_path / "workspace",
            source,
            WorktreeSourceRef(
                source_kind="project",
                source_node_id=None,
                source_commit=source.run_base_commit,
                source_tree=wrong_tree,
            ),
            tmp_path / "gitdir",
        )

    assert reset_called is False


def test_reused_worktree_rejects_missing_local_source_bundle_before_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
    reset_called = False

    def reject_reset(
        checkout_root: Path,
        source_commit: str,
        repo_root: Path,
        common_git_dir: Path,
        expected_git_dir: Path,
    ) -> None:
        nonlocal reset_called
        del checkout_root, source_commit, repo_root, common_git_dir, expected_git_dir
        reset_called = True
        raise AssertionError("reset should not run for missing source bundles")

    monkeypatch.setattr(
        worktree_reuse,
        "reset_reusable_worktree_checkout",
        reject_reset,
    )

    with pytest.raises(RuntimeError, match="bundle is missing"):
        worktree_reuse.reuse_worktree_workspace(
            tmp_path / "workspace",
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="upstream",
                source_commit=source.run_base_commit,
                source_tree=source.source_tree,
                candidate_sequence=1,
                bundle_path=tmp_path / "missing.bundle",
                bundle_sha256="0" * 64,
                bundle_size_bytes=1,
                bundle_ref="refs/crewplane/test/missing",
            ),
            tmp_path / "gitdir",
        )

    assert reset_called is False
