from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.runtime.workspace.worktree import (
    WorktreeSourceRef,
    create_worktree_workspace,
    remove_worktree_workspace,
)
from crewplane.runtime.workspace.worktree.lineage import (
    ensure_source_commit_available,
    verify_source_commit_available,
)
from tests.helpers.workspace_lineage_bundles import (
    create_full_bundle_chain,
)
from tests.helpers.workspace_service import (
    create_git_repo,
    git_commit_exists,
    run_git_text,
    workspace_plan,
)
from tests.unit.runtime.workspace.service_worktree_lineage_bundles_support import (
    import_owner_state,
    project_source_ref,
)


def test_worktree_workspace_imports_depth_three_full_bundle_chain(
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
    first, second, third = create_full_bundle_chain(
        repo,
        tmp_path / "first.bundle",
        tmp_path / "second.bundle",
        tmp_path / "third.bundle",
    )
    if any(git_commit_exists(repo, record.commit) for record in (first, second, third)):
        pytest.skip("git retained the test commits after pruning")
    state_path = import_owner_state(tmp_path, source)

    worktree = create_worktree_workspace(
        plan,
        "bundle-chain-import",
        source,
        WorktreeSourceRef(
            source_kind="node",
            source_node_id="third",
            source_commit=third.commit,
            source_tree=third.tree,
            candidate_sequence=1,
            bundle_path=third.path,
            bundle_sha256=third.sha256,
            bundle_size_bytes=third.size_bytes,
            bundle_ref=third.ref,
            upstream_sources=(
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
                            upstream_sources=(project_source_ref(source),),
                        ),
                    ),
                ),
            ),
        ),
        state_path=state_path,
    )

    try:
        assert git_commit_exists(repo, first.commit)
        assert git_commit_exists(repo, second.commit)
        assert git_commit_exists(repo, third.commit)
        assert (
            run_git_text(worktree.checkout_root, "rev-parse", "HEAD^{commit}")
            == third.commit
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert all(claim["phase"] == "removed" for claim in state["temporary_refs"])
    finally:
        remove_worktree_workspace(source, worktree.workspace_path)


def test_verify_source_commit_available_uses_bundles_for_source_verification(
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
    first, second = create_full_bundle_chain(
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
                    upstream_sources=(project_source_ref(source),),
                ),
            ),
        ),
    )

    assert not git_commit_exists(repo, first.commit)
    assert not git_commit_exists(repo, second.commit)
    assert (
        run_git_text(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        == source_refs_before
    )


def test_verify_project_source_does_not_mutate_repository_refs(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
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
    source_refs_before = run_git_text(repo, "for-each-ref", "--format=%(refname)")

    verify_source_commit_available(
        source,
        project_source_ref(source),
    )

    assert run_git_text(repo, "for-each-ref", "--format=%(refname)") == (
        source_refs_before
    )


def test_source_bundle_descriptor_validation_preserves_fail_fast_order(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    source = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    ).workspace_source
    assert source is not None
    bundle_path = tmp_path / "descriptor.bundle"
    bundle_path.write_bytes(b"not a bundle")
    digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    base_ref = WorktreeSourceRef(
        source_kind="node",
        source_node_id="upstream",
        source_commit=source.run_base_commit,
        source_tree=source.source_tree,
    )
    cases = (
        (base_ref, "bundle path is missing"),
        (
            replace(base_ref, bundle_path=tmp_path / "missing.bundle"),
            "bundle is missing",
        ),
        (replace(base_ref, bundle_path=bundle_path), "bundle digest is missing"),
        (
            replace(base_ref, bundle_path=bundle_path, bundle_sha256="0" * 64),
            "bundle digest mismatch",
        ),
        (
            replace(base_ref, bundle_path=bundle_path, bundle_sha256=digest),
            "bundle size is missing",
        ),
        (
            replace(
                base_ref,
                bundle_path=bundle_path,
                bundle_sha256=digest,
                bundle_size_bytes=bundle_path.stat().st_size + 1,
            ),
            "bundle size mismatch",
        ),
        (
            replace(
                base_ref,
                bundle_path=bundle_path,
                bundle_sha256=digest,
                bundle_size_bytes=bundle_path.stat().st_size,
            ),
            "bundle ref is missing",
        ),
    )

    for source_ref, expected_message in cases:
        with (
            pytest.raises(RuntimeError, match=expected_message),
            ensure_source_commit_available(
                source,
                source_ref,
                source_chain_verified=True,
            ),
        ):
            pass
