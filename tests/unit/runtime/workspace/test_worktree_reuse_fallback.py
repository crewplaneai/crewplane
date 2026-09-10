from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.workspace import materialization as workspace_materialization
from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.git import GitCommand
from crewplane.runtime.workspace.service import MaterializationLimiter
from crewplane.runtime.workspace.worktree import (
    cache as worktree_cache,
)
from crewplane.runtime.workspace.worktree import (
    materialization as worktree_materialization,
)
from crewplane.runtime.workspace.worktree import orchestration as worktree_orchestration
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from crewplane.runtime.workspace.worktree.cache import (
    WorktreeReuseCache,
)
from crewplane.runtime.workspace.worktree.temporary_refs import (
    reconcile_temporary_import_refs,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    run_git_text,
    workspace_invocation_context,
    workspace_output_manager,
)
from tests.helpers.workspace_worktree_reuse import (
    two_node_lineage_plan,
    workspace_request,
)


@pytest.mark.parametrize("archive_collision", [None, "file", "symlink"])
def test_same_worktree_reuse_failure_falls_back_to_fresh_checkout(
    archive_collision,
    monkeypatch: pytest.MonkeyPatch,
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
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()

    def fail_reuse(
        workspace_path: Path,
        source: object,
        source_ref: object,
        expected_git_dir: object,
        protected_ref_scopes: object,
        state_path: object,
    ) -> None:
        assert workspace_path.exists()
        assert source is not None
        assert source_ref is not None
        assert expected_git_dir is not None
        assert protected_ref_scopes is not None
        assert state_path is not None
        raise RuntimeError("reset verification failed")

    monkeypatch.setattr(
        worktree_materialization,
        "reuse_worktree_workspace",
        fail_reuse,
    )
    probe_calls = 0

    def record_probe(path: Path) -> SimpleNamespace:
        nonlocal probe_calls
        del path
        probe_calls += 1
        return SimpleNamespace(free=10**12)

    monkeypatch.setattr(workspace_materialization.shutil, "disk_usage", record_probe)

    if archive_collision is not None:
        archive_path = (
            output.create_node_dir(node_artifact_request("verify"))
            / "workspace-reuse-claim-workspace-state-generation-2.json"
        )
        existing = (
            tmp_path / "existing-claim.json"
            if archive_collision == "symlink"
            else archive_path
        )
        existing.write_text("preserved evidence")
        if archive_collision == "symlink":
            archive_path.symlink_to(existing)
        try:
            with pytest.raises(RuntimeError, match="archive already exists"):
                prepare_invocation_workspace(
                    workspace_request(plan, output, "verify", reuse_cache, limiter),
                    workspace_invocation_context(),
                )
            assert existing.read_text() == "preserved evidence"
            assert archive_path.is_symlink() is (archive_collision == "symlink")
        finally:
            reuse_cache.cleanup_all_best_effort()
        return

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
        assert "reset verification failed" in state["reuse"]["fallback_reason"]
        assert (
            state["reuse"]["abandoned_claim_artifact"]
            == "workspace-reuse-claim-workspace-state-generation-2.json"
        )
        assert probe_calls == 1
    finally:
        if second.workspace_path is not None:
            second.mark_succeeded(defer_cleanup=True)
        reuse_cache.cleanup_all_best_effort()


def test_failed_reuse_cleanup_retry_updates_abandoned_generation_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = two_node_lineage_plan(repo, tmp_path / "cache")
    source = plan.workspace_source
    assert source is not None
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    verify_dir = output.create_node_dir(node_artifact_request("verify"))
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    old_workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()

    def fail_reuse(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("unsafe reuse")

    removal_attempts = 0

    def fail_old_cleanup_once(
        source_arg: WorkspaceSourceSnapshot,
        workspace_path: Path,
        git_dir: Path,
    ) -> None:
        nonlocal removal_attempts
        removal_attempts += 1
        if removal_attempts == 1:
            raise RuntimeError("cleanup retry required")
        remove_worktree_workspace(source_arg, workspace_path, git_dir)

    monkeypatch.setattr(
        worktree_materialization,
        "reuse_worktree_workspace",
        fail_reuse,
    )
    monkeypatch.setattr(
        "crewplane.runtime.workspace.worktree.cache.remove_worktree_workspace",
        fail_old_cleanup_once,
    )

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert second.workspace_path is not None
    assert second.workspace_path != old_workspace_path
    abandoned_paths = tuple(verify_dir.glob("workspace-reuse-claim-*.json"))
    assert len(abandoned_paths) == 1
    abandoned_path = abandoned_paths[0]
    assert read_json_object(abandoned_path)["workspace"]["retention"] == "retained"
    assert old_workspace_path.exists()

    cleanup = reuse_cache.cleanup_all()

    assert cleanup.errors == ()
    assert abandoned_path in cleanup.updated_state_paths
    assert read_json_object(abandoned_path)["workspace"]["retention"] == "deleted"
    assert not old_workspace_path.exists()
    remove_worktree_workspace(source, second.workspace_path)


def test_failed_fresh_fallback_preserves_new_checkout_identity_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = two_node_lineage_plan(repo, tmp_path / "cache")
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    verify_dir = output.create_node_dir(node_artifact_request("verify"))
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    assert first.worktree_capture is not None
    old_workspace_path = first.workspace_path
    old_git_dir = first.worktree_capture.git_dir
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()

    def fail_reuse(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("unsafe reuse")

    original_remove = worktree_cache.remove_worktree_workspace

    def fail_old_cleanup(
        source: WorkspaceSourceSnapshot,
        workspace_path: Path,
        git_dir: Path,
    ) -> None:
        del source, git_dir
        assert workspace_path == old_workspace_path
        raise OSError("old cleanup failed")

    fresh_claims: list[tuple[Path, Path]] = []

    def fail_after_fresh_claim(
        git_top_level: str,
        protected_ref_scopes: tuple[str, ...] | None,
        source_ref: WorktreeSourceRef,
    ) -> NoReturn:
        del git_top_level, protected_ref_scopes, source_ref
        claim = read_json_object(verify_dir / "workspace-state.json")
        execution = claim["execution"]
        assert isinstance(execution, dict)
        workspace_path = execution["workspace_path"]
        git_dir = execution["worktree_git_dir"]
        assert isinstance(workspace_path, str)
        assert isinstance(git_dir, str)
        fresh_claims.append((Path(workspace_path), Path(git_dir)))
        raise RuntimeError("fresh post-claim failure")

    monkeypatch.setattr(
        worktree_materialization,
        "reuse_worktree_workspace",
        fail_reuse,
    )
    monkeypatch.setattr(worktree_cache, "remove_worktree_workspace", fail_old_cleanup)
    monkeypatch.setattr(
        worktree_orchestration,
        "protected_ref_snapshot_for_source",
        fail_after_fresh_claim,
    )

    with pytest.raises(RuntimeError, match="fresh post-claim failure"):
        prepare_invocation_workspace(
            workspace_request(plan, output, "verify", reuse_cache, limiter),
            workspace_invocation_context(),
        )

    assert len(fresh_claims) == 1
    fresh_workspace_path, fresh_git_dir = fresh_claims[0]
    state = read_json_object(verify_dir / "workspace-state.json")
    assert state["status"] == "failed"
    assert state["workspace"]["retention"] == "deleted"
    assert state["execution"]["worktree_git_dir"] == fresh_git_dir.as_posix()
    assert state["execution"]["worktree_git_dir"] != old_git_dir.as_posix()
    assert state["reuse"]["fallback"] is True
    assert not fresh_workspace_path.exists()
    assert old_workspace_path.exists()
    assert reuse_cache.owns(old_workspace_path)
    abandoned_paths = tuple(verify_dir.glob("workspace-reuse-claim-*.json"))
    assert len(abandoned_paths) == 1
    assert (
        read_json_object(abandoned_paths[0])["workspace"]["retained_reason"]
        == "unsafe_reuse_cleanup_failed"
    )

    monkeypatch.setattr(worktree_cache, "remove_worktree_workspace", original_remove)
    cleanup = reuse_cache.cleanup_all()

    assert cleanup.errors == ()
    assert not old_workspace_path.exists()


def test_preclaim_fallback_failure_preserves_temporary_ref_cleanup_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = two_node_lineage_plan(repo, tmp_path / "cache")
    source = plan.workspace_source
    assert source is not None
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    verify_dir = output.create_node_dir(node_artifact_request("verify"))
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    old_workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()

    def fail_reuse(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise RuntimeError("unsafe reuse")

    def fail_fresh_creation(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise RuntimeError("fresh creation failed")

    original_git_run = GitCommand.run

    def fail_temporary_ref_cleanup(
        command: GitCommand,
        *args: str,
    ) -> subprocess.CompletedProcess[bytes]:
        if (
            args[:3] == ("update-ref", "--no-deref", "-d")
            and len(args) > 3
            and "/imports/" in args[3]
        ):
            raise OSError("temporary ref cleanup failed")
        return original_git_run(command, *args)

    monkeypatch.setattr(
        worktree_materialization,
        "reuse_worktree_workspace",
        fail_reuse,
    )
    monkeypatch.setattr(
        worktree_materialization,
        "create_worktree_workspace",
        fail_fresh_creation,
    )
    monkeypatch.setattr(
        GitCommand,
        "run",
        fail_temporary_ref_cleanup,
    )

    with pytest.raises(RuntimeError, match="fresh creation failed"):
        prepare_invocation_workspace(
            workspace_request(plan, output, "verify", reuse_cache, limiter),
            workspace_invocation_context(),
        )

    state_path = verify_dir / "workspace-state.json"
    state = read_json_object(state_path)
    temporary_refs = state["temporary_refs"]
    assert isinstance(temporary_refs, list)
    assert len(temporary_refs) == 1
    claim = temporary_refs[0]
    assert isinstance(claim, dict)
    assert claim["phase"] == "prepared"
    assert run_git_text(repo, "rev-parse", claim["name"]) == claim["target_oid"]
    assert not old_workspace_path.exists()

    monkeypatch.setattr(GitCommand, "run", original_git_run)
    removed = reconcile_temporary_import_refs(
        state_path,
        repo,
        Path(source.common_git_dir),
        source.repository_id,
    )

    assert removed == 1
    assert read_json_object(state_path)["temporary_refs"][0]["phase"] == "removed"
