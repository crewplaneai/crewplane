from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.service import MaterializationLimiter
from crewplane.runtime.workspace.worktree import reset as worktree_reset
from crewplane.runtime.workspace.worktree import reuse as worktree_reuse
from crewplane.runtime.workspace.worktree.cache import (
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


def test_same_worktree_reuse_rejects_retargeted_git_file_before_reset(
    tmp_path: Path,
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
            output.create_node_dir(node_artifact_request("verify"))
            / "workspace-state.json"
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

    def ignore_head_policy(*args: object) -> None:
        del args

    monkeypatch.setattr(
        worktree_reset,
        "reject_common_git_policy_drift",
        ignore_common_git_policy_drift,
    )
    monkeypatch.setattr(
        worktree_reset,
        "reject_attached_head_after_safe_detachment",
        ignore_head_policy,
    )
    monkeypatch.setattr(
        worktree_reset,
        "prove_detached_head",
        ignore_head_policy,
    )

    with pytest.raises(RuntimeError, match="Git info dir must be a real directory"):
        worktree_reset.reset_reusable_worktree_checkout(
            checkout_root,
            "a" * 40,
            repo_root,
            common_git_dir,
            expected_git_dir,
        )


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

    with pytest.raises(
        RuntimeError, match="project source descriptor is contradictory"
    ):
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
