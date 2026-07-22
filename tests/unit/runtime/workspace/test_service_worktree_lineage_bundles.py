from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from crewplane.runtime.workspace.git import GitCommand
from crewplane.runtime.workspace.worktree import (
    WorktreeSourceRef,
    create_worktree_workspace,
    remove_worktree_workspace,
)
from crewplane.runtime.workspace.worktree.lineage import (
    export_bundle,
    verify_source_commit_available,
)
from crewplane.runtime.workspace.worktree.protected_refs import (
    ProtectedRefSnapshot,
)
from crewplane.runtime.workspace.worktree.types import WorktreeCaptureRequest
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
    result_ref = "refs/crewplane/tests/result"
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


def test_verify_source_commit_available_uses_bundles_for_source_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    source_refs_before = run_git_text(
        repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
    )
    original_run = GitCommand.run
    verification_fetches: list[tuple[str, ...]] = []

    def reject_source_ref_update(
        self: GitCommand,
        *args: str,
    ) -> subprocess.CompletedProcess[bytes]:
        if self.cwd == repo and args and args[0] == "update-ref":
            raise AssertionError("lineage verification mutated a source ref")
        if self.cwd != repo and args and args[0] == "fetch":
            verification_fetches.append(args)
        return original_run(self, *args)

    monkeypatch.setattr(GitCommand, "run", reject_source_ref_update)

    verify_source_commit_available(
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

    assert verification_fetches
    assert all("--no-auto-maintenance" in args for args in verification_fetches)
    assert not git_commit_exists(repo, first.commit)
    assert not git_commit_exists(repo, second.commit)
    assert (
        run_git_text(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        == source_refs_before
    )


def test_verify_source_commit_available_does_not_import_base_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    base_parent = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    (repo / "README.md").write_text("current base\n", encoding="utf-8")
    run_git_text(repo, "add", "README.md")
    run_git_text(repo, "commit", "-m", "current base")
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    base_fetch_observed = False
    original_run = GitCommand.run

    def assert_base_history_is_absent(
        self: GitCommand,
        *args: str,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal base_fetch_observed
        result = original_run(self, *args)
        if args and args[0] == "fetch" and repo.as_posix() in args:
            base_fetch_observed = True
            assert not git_commit_exists(self.cwd, base_parent)
        return result

    monkeypatch.setattr(GitCommand, "run", assert_base_history_is_absent)

    verify_source_commit_available(
        source,
        WorktreeSourceRef(
            source_kind="project",
            source_node_id=None,
            source_commit=source.run_base_commit,
            source_tree=source.source_tree,
        ),
    )

    assert base_fetch_observed


def test_verify_source_commit_available_rejects_ambient_omitted_upstream(
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
    first, second = create_prerequisite_bundle_chain(
        repo,
        tmp_path / "first.bundle",
        tmp_path / "second.bundle",
    )
    ambient_first_ref = "refs/crewplane/test/ambient-first"
    ambient_second_ref = "refs/crewplane/test/ambient-second"
    run_git_text(
        repo, "fetch", first.path.as_posix(), f"{first.ref}:{ambient_first_ref}"
    )
    run_git_text(
        repo,
        "fetch",
        second.path.as_posix(),
        f"{second.ref}:{ambient_second_ref}",
    )
    run_git_text(repo, "update-ref", "-d", ambient_first_ref)
    run_git_text(repo, "update-ref", "-d", ambient_second_ref)
    assert git_commit_exists(repo, first.commit)
    assert git_commit_exists(repo, second.commit)
    source_refs_before = run_git_text(
        repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
    )

    with pytest.raises(RuntimeError) as exc_info:
        verify_source_commit_available(
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
            ),
        )

    message = str(exc_info.value)
    assert message.startswith(
        "Workspace lineage source verification failed while validating recorded "
        "Git artifacts:"
    )
    assert "Git did not provide diagnostic output" not in message
    assert "crewplane-lineage-verify-" not in message
    assert "Command '['git'" not in message
    assert (
        run_git_text(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        == source_refs_before
    )


def test_verify_source_commit_available_rejects_ambient_project_commit(
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
    (repo / "ambient.txt").write_text("ambient\n", encoding="utf-8")
    run_git_text(repo, "add", "ambient.txt")
    run_git_text(repo, "commit", "-m", "ambient")
    ambient_commit = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    ambient_tree = run_git_text(repo, "rev-parse", "HEAD^{tree}")
    source_refs_before = run_git_text(
        repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
    )

    with pytest.raises(RuntimeError, match="requires a recorded bundle"):
        verify_source_commit_available(
            source,
            WorktreeSourceRef(
                source_kind="project",
                source_node_id=None,
                source_commit=ambient_commit,
                source_tree=ambient_tree,
            ),
        )

    assert (
        run_git_text(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        == source_refs_before
    )


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
                bundle_ref="refs/crewplane/test/missing",
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
