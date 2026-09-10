from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from crewplane.runtime.workspace.git import GitCommand
from crewplane.runtime.workspace.invocation import invocation_slug
from crewplane.runtime.workspace.worktree import (
    WorktreeSourceRef,
    create_worktree_workspace,
    remove_worktree_workspace,
)
from crewplane.runtime.workspace.worktree.lineage import (
    ensure_source_commit_available,
    export_bundle,
    worktree_protected_ref_scopes,
)
from crewplane.runtime.workspace.worktree.protected_refs import (
    ProtectedRefSnapshot,
)
from crewplane.runtime.workspace.worktree.temporary_refs import (
    TemporaryRefOwner,
    reconcile_temporary_import_refs,
)
from crewplane.runtime.workspace.worktree.types import (
    WorktreeCaptureRequest,
)
from tests.helpers.workspace_lineage_bundles import (
    create_full_bundle_chain,
    create_pruned_result_bundle,
    create_result_bundle,
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
    state_path = import_owner_state(tmp_path, source)
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
            upstream_sources=(project_source_ref(source),),
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
    state_path = import_owner_state(tmp_path, source)
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
        upstream_sources=(project_source_ref(source),),
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


def test_temporary_import_ref_cleanup_runs_in_reverse_and_notes_later_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    first, second = create_full_bundle_chain(
        repo,
        tmp_path / "first.bundle",
        tmp_path / "second.bundle",
    )
    if git_commit_exists(repo, first.commit) or git_commit_exists(repo, second.commit):
        pytest.skip("git retained the test commits after pruning")
    source_ref = WorktreeSourceRef(
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
    )
    original_run = GitCommand.run
    cleanup_targets: list[str] = []

    def fail_import_ref_cleanup(
        command: GitCommand,
        *args: str,
    ) -> subprocess.CompletedProcess[bytes]:
        if args[:3] == ("update-ref", "--no-deref", "-d"):
            cleanup_targets.append(args[4])
            raise RuntimeError(f"cleanup failed for {args[4]}")
        return original_run(command, *args)

    monkeypatch.setattr(GitCommand, "run", fail_import_ref_cleanup)

    with (
        pytest.raises(RuntimeError, match=f"cleanup failed for {second.commit}") as exc,
        ensure_source_commit_available(
            source,
            source_ref,
            TemporaryRefOwner(import_owner_state(tmp_path, source)),
        ),
    ):
        pass

    assert cleanup_targets == [second.commit, first.commit]
    assert getattr(exc.value, "__notes__", ()) == [
        "Additional workspace temporary import ref cleanup failed: "
        f"cleanup failed for {first.commit}"
    ]


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
        upstream_sources=(project_source_ref(source),),
    )
    run_git_text(repo, "update-ref", result_ref, source.run_base_commit)
    slug = "moved-lineage-ref"
    state_path = import_owner_state(tmp_path, source)

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


@pytest.mark.parametrize("target_exists", [False, True], ids=("dangling", "live"))
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
    state_path = import_owner_state(tmp_path, source)
    slug = invocation_slug("implement", "alpha", None, 1)
    import_ref = (
        f"refs/crewplane/runs/workspace-run-001/imports/implement/{slug}/temporary"
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


def test_temporary_ref_reconciliation_keeps_earlier_success_when_later_claim_fails(
    tmp_path: Path,
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
    state_path = import_owner_state(tmp_path, source)
    slug = invocation_slug("implement", "alpha", None, 1)
    first_ref = f"refs/crewplane/runs/workspace-run-001/imports/implement/{slug}/first"
    second_ref = (
        f"refs/crewplane/runs/workspace-run-001/imports/implement/{slug}/second"
    )
    expected_oid = source.run_base_commit
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    run_git_text(repo, "add", "later.txt")
    run_git_text(repo, "commit", "-m", "later")
    moved_oid = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    run_git_text(repo, "update-ref", first_ref, expected_oid)
    run_git_text(repo, "update-ref", second_ref, moved_oid)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["temporary_refs"] = [
        {
            "name": first_ref,
            "target_oid": expected_oid,
            "phase": "prepared",
            "repository_id": source.repository_id,
        },
        {
            "name": second_ref,
            "target_oid": expected_oid,
            "phase": "prepared",
            "repository_id": source.repository_id,
        },
    ]
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="moved and was retained"):
        reconcile_temporary_import_refs(
            state_path,
            repo,
            repo / ".git",
            source.repository_id,
        )

    assert not run_git_text(repo, "for-each-ref", "--format=%(refname)", first_ref)
    assert run_git_text(repo, "rev-parse", second_ref) == moved_oid
    claims = json.loads(state_path.read_text(encoding="utf-8"))["temporary_refs"]
    assert claims[0]["phase"] == "removed"
    assert claims[1]["phase"] == "prepared"


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
