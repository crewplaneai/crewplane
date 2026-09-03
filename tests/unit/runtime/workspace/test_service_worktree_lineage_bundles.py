from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from crewplane.artifacts.workspace.chain_validation import (
    verify_persisted_workspace_result_chain,
    verify_workspace_source_chain,
)
from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.workspace.git import GitCommand
from crewplane.runtime.workspace.worktree import (
    WorktreeSourceRef,
    create_worktree_workspace,
    remove_worktree_workspace,
)
from crewplane.runtime.workspace.worktree.lineage import (
    TemporaryRefOwner,
    ensure_source_commit_available,
    export_bundle,
    reconcile_temporary_import_refs,
    verify_source_commit_available,
    worktree_protected_ref_scopes,
)
from crewplane.runtime.workspace.worktree.protected_refs import (
    ProtectedRefSnapshot,
)
from crewplane.runtime.workspace.worktree.types import (
    WorkspaceSourceKind,
    WorktreeCaptureRequest,
)
from tests.helpers.workspace_lineage_bundles import (
    create_full_bundle_chain,
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


def _project_source_ref(source: WorkspaceSourceSnapshot) -> WorktreeSourceRef:
    return WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=source.run_base_commit,
        source_tree=source.source_tree,
    )


def _import_owner_state(tmp_path: Path, source: WorkspaceSourceSnapshot) -> Path:
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text(
        json.dumps(
            {
                "run_id": "run-001",
                "run_key_name": "workspace-run-001",
                "node_id": "implement",
                "task_id": "alpha",
                "role": "executor",
                "round_num": 1,
                "audit_round_num": None,
                "git": {"repo_id": source.repository_id},
            }
        ),
        encoding="utf-8",
    )
    return state_path


def test_worktree_workspace_imports_missing_bundle_source_commit(
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
    result_commit, tree, result_ref, bundle_path, bundle_sha256 = (
        create_pruned_result_bundle(tmp_path, repo)
    )
    if git_commit_exists(repo, result_commit):
        pytest.skip("git retained the test commit after pruning")
    state_path = _import_owner_state(tmp_path, source)
    original_run = GitCommand.run
    pruned_before_add = False

    def run_after_prune(
        command: GitCommand,
        *args: str,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal pruned_before_add
        if args[:2] == ("worktree", "add") and not pruned_before_add:
            imported_refs = run_git_text(
                repo,
                "for-each-ref",
                "--format=%(objectname)",
                f"refs/crewplane/runs/{plan.run_key_name}/imports",
            ).splitlines()
            assert result_commit in imported_refs
            run_git_text(repo, "reflog", "expire", "--expire=now", "--all")
            run_git_text(repo, "gc", "--prune=now")
            pruned_before_add = True
        return original_run(command, *args)

    monkeypatch.setattr(GitCommand, "run", run_after_prune)

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
            upstream_sources=(_project_source_ref(source),),
        ),
        state_path=state_path,
    )

    try:
        assert (
            run_git_text(worktree.checkout_root, "rev-parse", "HEAD^{commit}")
            == result_commit
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["temporary_refs"][0]["phase"] == "removed"
    finally:
        remove_worktree_workspace(source, worktree.workspace_path)


def test_existing_bundle_commit_is_temporarily_rooted_during_consumption(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, result_ref, bundle_path, bundle_sha256 = create_result_bundle(
        tmp_path,
        repo,
        "ambient-result",
    )
    run_git_text(repo, "update-ref", "-d", result_ref)
    run_git_text(repo, "reflog", "expire", "--expire=now", "--all")
    assert git_commit_exists(repo, result_commit)
    state_path = _import_owner_state(tmp_path, source)
    source_ref = WorktreeSourceRef(
        source_kind="node",
        source_node_id="upstream",
        source_commit=result_commit,
        source_tree=tree,
        candidate_sequence=1,
        bundle_path=bundle_path,
        bundle_sha256=bundle_sha256,
        bundle_size_bytes=bundle_path.stat().st_size,
        bundle_ref=result_ref,
        upstream_sources=(_project_source_ref(source),),
    )

    with ensure_source_commit_available(
        source,
        source_ref,
        TemporaryRefOwner(state_path),
    ):
        import_refs = run_git_text(
            repo,
            "for-each-ref",
            "--format=%(objectname)",
            f"refs/crewplane/runs/{plan.run_key_name}/imports",
        ).splitlines()
        assert result_commit in import_refs
        run_git_text(repo, "gc", "--prune=now")
        assert git_commit_exists(repo, result_commit)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["temporary_refs"][0]["phase"] == "removed"
    assert not run_git_text(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        f"refs/crewplane/runs/{plan.run_key_name}/imports",
    )
    run_git_text(repo, "reflog", "expire", "--expire=now", "--all")
    run_git_text(repo, "gc", "--prune=now")
    assert not git_commit_exists(repo, result_commit)


def test_worktree_preparation_rejects_live_lineage_ref_at_wrong_commit(
    tmp_path: Path,
) -> None:
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
        tmp_path,
        repo,
        "moved-lineage-ref",
    )
    source_ref = WorktreeSourceRef(
        source_kind="node",
        source_node_id="upstream",
        source_commit=result_commit,
        source_tree=tree,
        candidate_sequence=1,
        bundle_path=bundle_path,
        bundle_sha256=bundle_sha256,
        bundle_size_bytes=bundle_path.stat().st_size,
        bundle_ref=result_ref,
        upstream_sources=(_project_source_ref(source),),
    )
    run_git_text(repo, "update-ref", result_ref, source.run_base_commit)
    slug = "moved-lineage-ref"
    state_path = _import_owner_state(tmp_path, source)

    with pytest.raises(RuntimeError, match="consumed lineage ref"):
        create_worktree_workspace(
            plan,
            slug,
            source,
            source_ref,
            worktree_protected_ref_scopes(
                plan,
                source_ref,
                "implement",
                slug,
            ),
            state_path=state_path,
        )

    workspace_path = (
        cache_root / "workspaces" / source.repository_id / plan.run_key_name / slug
    )
    assert not workspace_path.exists()
    assert run_git_text(repo, "rev-parse", result_ref) == source.run_base_commit


@pytest.mark.parametrize("target_exists", (False, True), ids=("dangling", "live"))
def test_temporary_ref_cleanup_rejects_symbolic_ref_without_touching_target(
    tmp_path: Path,
    target_exists: bool,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    source = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    ).workspace_source
    assert source is not None
    state_path = _import_owner_state(tmp_path, source)
    import_ref = (
        "refs/crewplane/runs/workspace-run-001/imports/implement/"
        "implement-alpha-round1/temporary"
    )
    target_ref = "refs/heads/user-work"
    target_oid = source.run_base_commit
    if target_exists:
        run_git_text(repo, "update-ref", target_ref, target_oid)
    run_git_text(repo, "symbolic-ref", import_ref, target_ref)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["temporary_refs"] = [
        {
            "name": import_ref,
            "target_oid": target_oid,
            "phase": "prepared",
            "repository_id": source.repository_id,
        }
    ]
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="symbolic"):
        reconcile_temporary_import_refs(
            state_path,
            repo,
            repo / ".git",
            source.repository_id,
        )

    assert run_git_text(repo, "symbolic-ref", import_ref) == target_ref
    listed_target = run_git_text(
        repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        target_ref,
    )
    assert listed_target == (f"{target_ref} {target_oid}" if target_exists else "")
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["temporary_refs"][0]["phase"]
        == "prepared"
    )


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
    state_path = _import_owner_state(tmp_path, source)

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
                            upstream_sources=(_project_source_ref(source),),
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
                    upstream_sources=(_project_source_ref(source),),
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
        _project_source_ref(source),
    )

    assert run_git_text(repo, "for-each-ref", "--format=%(refname)") == (
        source_refs_before
    )


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

    with pytest.raises(
        RuntimeError, match="project source descriptor is contradictory"
    ):
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

    with pytest.raises(RuntimeError, match="result tree mismatch"):
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
                upstream_sources=(_project_source_ref(source),),
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
    tampered = bytearray(bundle_path.read_bytes())
    tampered[-1] ^= 1
    bundle_path.write_bytes(tampered)

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

    with pytest.raises(RuntimeError, match="bundle ref or target OID mismatch"):
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

    with pytest.raises(RuntimeError, match="bundle descriptor is incomplete"):
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


def test_isolated_chain_verifier_rejects_descriptor_shape_and_bundle_claims(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, result_ref, bundle_path, bundle_sha256 = create_result_bundle(
        tmp_path, repo, "validation-errors"
    )
    project = _project_source_ref(source)
    valid = WorktreeSourceRef(
        source_kind="node",
        source_node_id="upstream",
        source_commit=result_commit,
        source_tree=tree,
        candidate_sequence=1,
        bundle_path=bundle_path,
        bundle_sha256=bundle_sha256,
        bundle_size_bytes=bundle_path.stat().st_size,
        bundle_ref=result_ref,
        upstream_sources=(project,),
    )

    with pytest.raises(RuntimeError, match="bundle kind is invalid"):
        verify_workspace_source_chain(
            source,
            replace(valid, source_kind=cast(WorkspaceSourceKind, "invalid")),
        )
    with pytest.raises(RuntimeError, match="lacks a source node"):
        verify_workspace_source_chain(source, replace(valid, source_node_id=None))
    with pytest.raises(RuntimeError, match="exactly one parent source"):
        verify_workspace_source_chain(source, replace(valid, upstream_sources=()))
    with pytest.raises(RuntimeError, match="exactly one parent source"):
        verify_workspace_source_chain(
            source,
            replace(valid, upstream_sources=(project, project)),
        )
    with pytest.raises(RuntimeError, match="bundle size mismatch"):
        verify_workspace_source_chain(
            source,
            replace(valid, bundle_size_bytes=bundle_path.stat().st_size + 1),
        )

    bundle_link = tmp_path / "linked.bundle"
    bundle_link.symlink_to(bundle_path)
    with pytest.raises(RuntimeError, match="must be a regular file"):
        verify_workspace_source_chain(source, replace(valid, bundle_path=bundle_link))

    invalid_header = tmp_path / "incomplete.bundle"
    invalid_header.write_bytes(b"# v2 git bundle\n")
    with pytest.raises(RuntimeError, match="header is incomplete"):
        verify_workspace_source_chain(
            source,
            replace(
                valid,
                bundle_path=invalid_header,
                bundle_sha256=hashlib.sha256(invalid_header.read_bytes()).hexdigest(),
                bundle_size_bytes=invalid_header.stat().st_size,
            ),
        )

    invalid_source = source.model_copy(update={"object_format": "invalid"})
    with pytest.raises(RuntimeError, match="Unsupported workspace object format"):
        verify_workspace_source_chain(invalid_source, project)


def test_isolated_chain_verifier_rejects_multiple_refs_and_wrong_parent(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    result_commit, tree, result_ref, _bundle_path, _bundle_sha256 = (
        create_result_bundle(tmp_path, repo, "multiple-refs")
    )
    extra_ref = "refs/crewplane/test/extra"
    run_git_text(repo, "update-ref", extra_ref, source.run_base_commit)
    multiple_bundle = tmp_path / "multiple.bundle"
    run_git_text(
        repo,
        "bundle",
        "create",
        multiple_bundle.as_posix(),
        result_ref,
        extra_ref,
    )
    with pytest.raises(RuntimeError, match="exactly one ref"):
        verify_workspace_source_chain(
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="upstream",
                source_commit=result_commit,
                source_tree=tree,
                bundle_path=multiple_bundle,
                bundle_sha256=hashlib.sha256(multiple_bundle.read_bytes()).hexdigest(),
                bundle_size_bytes=multiple_bundle.stat().st_size,
                bundle_ref=result_ref,
                upstream_sources=(_project_source_ref(source),),
            ),
        )

    first, second = create_full_bundle_chain(
        repo,
        tmp_path / "first-parent.bundle",
        tmp_path / "second-parent.bundle",
    )
    with pytest.raises(RuntimeError, match="unexpected parent"):
        verify_workspace_source_chain(
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="second",
                source_commit=second.commit,
                source_tree=second.tree,
                bundle_path=second.path,
                bundle_sha256=second.sha256,
                bundle_size_bytes=second.size_bytes,
                bundle_ref=second.ref,
                upstream_sources=(_project_source_ref(source),),
            ),
        )
    assert first.commit != source.run_base_commit


def test_persisted_chain_verifier_rejects_unsafe_descriptor_fields(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "descriptor.bundle").write_bytes(b"descriptor")
    payload: dict[str, object] = {
        "node_id": "upstream",
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": source.run_base_commit,
            "tree": source.source_tree,
            "upstream_sources": [],
        },
        "result": {
            "result_commit": "c" * 40,
            "result_tree": "d" * 40,
        },
        "refs": {"result": "refs/crewplane/test/result"},
        "bundle": {
            "path": "descriptor.bundle",
            "sha256": "e" * 64,
            "size_bytes": 42,
        },
    }

    source_payload = payload["source"]
    assert isinstance(source_payload, dict)
    source_payload["upstream_sources"] = "invalid"
    with pytest.raises(RuntimeError, match="descriptor chain is invalid"):
        verify_persisted_workspace_result_chain(source, run_dir, payload)

    source_payload["upstream_sources"] = []
    bundle = payload["bundle"]
    assert isinstance(bundle, dict)
    bundle["path"] = "/absolute.bundle"
    with pytest.raises(RuntimeError, match="bundle path is unsafe"):
        verify_persisted_workspace_result_chain(source, run_dir, payload)

    bundle["path"] = "missing.bundle"
    with pytest.raises(RuntimeError, match="bundle path is missing"):
        verify_persisted_workspace_result_chain(source, run_dir, payload)

    payload["node_id"] = ""
    with pytest.raises(RuntimeError, match="lacks a required string"):
        verify_persisted_workspace_result_chain(source, run_dir, payload)

    payload["node_id"] = "upstream"
    bundle["path"] = "descriptor.bundle"
    bundle["size_bytes"] = True
    with pytest.raises(RuntimeError, match="lacks a required size"):
        verify_persisted_workspace_result_chain(source, run_dir, payload)
