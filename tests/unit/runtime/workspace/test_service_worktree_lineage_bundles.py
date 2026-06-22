from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from orchestrator_cli.runtime.workspace.worktree import (
    WorktreeSourceRef,
    create_worktree_workspace,
    remove_worktree_workspace,
)
from orchestrator_cli.runtime.workspace.worktree.lineage import export_bundle
from orchestrator_cli.runtime.workspace.worktree.protected_refs import (
    ProtectedRefSnapshot,
)
from orchestrator_cli.runtime.workspace.worktree.types import WorktreeCaptureRequest
from tests.helpers.workspace_lineage_bundles import (
    create_prerequisite_bundle_chain,
    create_pruned_result_bundle,
    create_result_bundle,
)
from tests.helpers.workspace_service import (
    create_git_repo,
    git_commit_exists,
    run_git_text,
    workspace_plan,
)


def test_worktree_workspace_imports_missing_bundle_source_commit(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, result_ref, bundle_path, bundle_sha256 = (
        create_pruned_result_bundle(tmp_path, repo)
    )
    if git_commit_exists(repo, result_commit):
        pytest.skip("git retained the test commit after pruning")

    worktree = create_worktree_workspace(
        plan,
        "bundle-import",
        source,
        WorktreeSourceRef(
            source_kind="node",
            source_node_id="upstream",
            source_commit=result_commit,
            source_tree=tree,
            candidate_sequence=1,
            bundle_path=bundle_path,
            bundle_sha256=bundle_sha256,
            bundle_size_bytes=bundle_path.stat().st_size,
            bundle_ref=result_ref,
        ),
    )

    try:
        assert (
            run_git_text(worktree.checkout_root, "rev-parse", "HEAD^{commit}")
            == result_commit
        )
    finally:
        remove_worktree_workspace(source, worktree.workspace_path)


def test_export_bundle_rejects_symlinked_bundle_directory(tmp_path: Path) -> None:
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
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    outside_dir = tmp_path / "outside-bundles"
    outside_dir.mkdir()
    (stage_dir / "workspace-bundles").symlink_to(
        outside_dir,
        target_is_directory=True,
    )
    request = WorktreeCaptureRequest(
        plan=plan,
        source=source,
        source_ref=WorktreeSourceRef(
            source_kind="project",
            source_node_id=None,
            source_commit=source.run_base_commit,
            source_tree=source.source_tree,
        ),
        workspace_path=tmp_path / "workspace",
        checkout_root=repo,
        git_dir=repo / ".git",
        node_id="implement",
        task_id="alpha",
        state_path=stage_dir / "workspace-state.json",
        slug="implement-alpha-round1",
        protected_refs=ProtectedRefSnapshot(scopes=(), refs=()),
    )

    with pytest.raises(RuntimeError, match="bundle directory must be a real directory"):
        export_bundle(request, "refs/heads/main")


def test_export_bundle_rejects_symlinked_bundle_file(tmp_path: Path) -> None:
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
    stage_dir = tmp_path / "stage"
    bundle_dir = stage_dir / "workspace-bundles"
    bundle_dir.mkdir(parents=True)
    outside_file = tmp_path / "outside.bundle"
    outside_file.write_text("outside\n", encoding="utf-8")
    slug = "implement-alpha-round1"
    (bundle_dir / f"{slug}.bundle").symlink_to(outside_file)
    result_ref = "refs/orchestrator-cli/tests/result"
    run_git_text(repo, "update-ref", result_ref, source.run_base_commit)
    request = WorktreeCaptureRequest(
        plan=plan,
        source=source,
        source_ref=WorktreeSourceRef(
            source_kind="project",
            source_node_id=None,
            source_commit=source.run_base_commit,
            source_tree=source.source_tree,
        ),
        workspace_path=tmp_path / "workspace",
        checkout_root=repo,
        git_dir=repo / ".git",
        node_id="implement",
        task_id="alpha",
        state_path=stage_dir / "workspace-state.json",
        slug=slug,
        protected_refs=ProtectedRefSnapshot(scopes=(), refs=()),
    )

    with pytest.raises(RuntimeError, match="bundle path must not be a symlink"):
        export_bundle(request, result_ref)

    assert outside_file.read_text(encoding="utf-8") == "outside\n"


def test_worktree_workspace_imports_depth_two_bundle_chain(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    first, second = create_prerequisite_bundle_chain(
        repo,
        tmp_path / "first.bundle",
        tmp_path / "second.bundle",
    )
    if git_commit_exists(repo, first.commit) or git_commit_exists(repo, second.commit):
        pytest.skip("git retained the test commits after pruning")

    worktree = create_worktree_workspace(
        plan,
        "bundle-chain-import",
        source,
        WorktreeSourceRef(
            source_kind="node",
            source_node_id="second",
            source_commit=second.commit,
            source_tree=second.tree,
            candidate_sequence=1,
            bundle_path=second.path,
            bundle_sha256=second.sha256,
            bundle_size_bytes=second.size_bytes,
            bundle_ref=second.ref,
            upstream_sources=(
                WorktreeSourceRef(
                    source_kind="node",
                    source_node_id="first",
                    source_commit=first.commit,
                    source_tree=first.tree,
                    candidate_sequence=1,
                    bundle_path=first.path,
                    bundle_sha256=first.sha256,
                    bundle_size_bytes=first.size_bytes,
                    bundle_ref=first.ref,
                ),
            ),
        ),
    )

    try:
        assert git_commit_exists(repo, first.commit)
        assert git_commit_exists(repo, second.commit)
        assert (
            run_git_text(worktree.checkout_root, "rev-parse", "HEAD^{commit}")
            == second.commit
        )
    finally:
        remove_worktree_workspace(source, worktree.workspace_path)


def test_worktree_workspace_rejects_imported_source_tree_mismatch(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, result_ref, bundle_path, bundle_sha256 = (
        create_pruned_result_bundle(tmp_path, repo)
    )
    if git_commit_exists(repo, result_commit):
        pytest.skip("git retained the test commit after pruning")
    wrong_tree = "f" * 40
    assert wrong_tree != tree

    with pytest.raises(RuntimeError, match="source tree mismatch"):
        create_worktree_workspace(
            plan,
            "bad-imported-source-tree",
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="upstream",
                source_commit=result_commit,
                source_tree=wrong_tree,
                candidate_sequence=1,
                bundle_path=bundle_path,
                bundle_sha256=bundle_sha256,
                bundle_size_bytes=bundle_path.stat().st_size,
                bundle_ref=result_ref,
            ),
        )


def test_worktree_workspace_rejects_missing_local_source_bundle(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, result_ref, bundle_path, bundle_sha256 = create_result_bundle(
        tmp_path, repo, "missing-local-source"
    )
    assert git_commit_exists(repo, result_commit)
    bundle_size = bundle_path.stat().st_size
    bundle_path.unlink()
    workspace_path = (
        cache_root
        / "workspaces"
        / source.repository_id
        / plan.run_key_name
        / "missing-local-source-bundle"
    )

    with pytest.raises(RuntimeError, match="bundle is missing"):
        create_worktree_workspace(
            plan,
            "missing-local-source-bundle",
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="upstream",
                source_commit=result_commit,
                source_tree=tree,
                candidate_sequence=1,
                bundle_path=bundle_path,
                bundle_sha256=bundle_sha256,
                bundle_size_bytes=bundle_size,
                bundle_ref=result_ref,
            ),
        )

    assert not workspace_path.exists()


def test_worktree_workspace_rejects_tampered_local_source_bundle(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, result_ref, bundle_path, bundle_sha256 = create_result_bundle(
        tmp_path, repo, "tampered-local-source"
    )
    assert git_commit_exists(repo, result_commit)
    bundle_size = bundle_path.stat().st_size
    bundle_path.write_text("not a git bundle\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="bundle digest mismatch"):
        create_worktree_workspace(
            plan,
            "tampered-local-source-bundle",
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="upstream",
                source_commit=result_commit,
                source_tree=tree,
                candidate_sequence=1,
                bundle_path=bundle_path,
                bundle_sha256=bundle_sha256,
                bundle_size_bytes=bundle_size,
                bundle_ref=result_ref,
            ),
        )


def test_worktree_workspace_rejects_local_source_bundle_ref_mismatch(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, _result_ref, bundle_path, bundle_sha256 = create_result_bundle(
        tmp_path, repo, "wrong-ref-local-source"
    )
    assert git_commit_exists(repo, result_commit)

    with pytest.raises(RuntimeError, match="bundle ref mismatch"):
        create_worktree_workspace(
            plan,
            "wrong-ref-local-source-bundle",
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="upstream",
                source_commit=result_commit,
                source_tree=tree,
                candidate_sequence=1,
                bundle_path=bundle_path,
                bundle_sha256=bundle_sha256,
                bundle_size_bytes=bundle_path.stat().st_size,
                bundle_ref="refs/orchestrator-cli/test/missing",
            ),
        )


def test_worktree_workspace_rejects_candidate_source_without_bundle(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None

    with pytest.raises(RuntimeError, match="bundle path is missing"):
        create_worktree_workspace(
            plan,
            "candidate-missing-bundle",
            source,
            WorktreeSourceRef(
                source_kind="candidate",
                source_node_id="implement",
                source_commit=source.run_base_commit,
                source_tree=source.source_tree,
                candidate_sequence=2,
            ),
        )
