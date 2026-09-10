from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import crewplane.runtime.workspace.git as workspace_git
from crewplane.runtime.workspace.cleanup import (
    WorkspaceCleanupFilter,
    cleanup_workspace_cache,
)
from crewplane.runtime.workspace.snapshot import remove_workspace_path
from crewplane.runtime.workspace.worktree import cleanup as worktree_cleanup
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from tests.helpers.workspace_service import workspace_plan
from tests.unit.runtime.workspace.cleanup_support import (
    cache_workspace_path,
    cleanup_git_repo,
    run_cleanup_git,
)


def test_remove_workspace_path_does_not_chmod_hardlinked_files(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    outside.chmod(0o640)
    original_mode = stat.S_IMODE(outside.stat().st_mode)
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    hardlink_path = workspace_path / "linked.txt"
    try:
        os.link(outside, hardlink_path)
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")

    remove_workspace_path(workspace_path)

    assert not workspace_path.exists()
    assert outside.read_text(encoding="utf-8") == "keep"
    assert stat.S_IMODE(outside.stat().st_mode) == original_mode


def test_remove_worktree_workspace_unlinks_top_level_symlink_without_git_remove(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = cleanup_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    outside = tmp_path / "outside"
    outside.mkdir()
    external_checkout = outside / "checkout"
    run_cleanup_git(
        repo, "worktree", "add", "--detach", external_checkout.as_posix(), "HEAD"
    )
    workspace_link = cache_workspace_path(tmp_path, "workspaces", "run-1", "linked")
    workspace_link.parent.mkdir(parents=True)
    try:
        workspace_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    remove_worktree_workspace(source, workspace_link)

    assert not workspace_link.exists()
    assert not workspace_link.is_symlink()
    assert external_checkout.exists()
    assert external_checkout.as_posix() in run_cleanup_git(
        repo, "worktree", "list", "--porcelain"
    )


def test_remove_worktree_workspace_retains_then_removes_same_unsafe_path_by_mode(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = cleanup_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
    workspace_link = tmp_path / "workspace"
    try:
        workspace_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(
        RuntimeError,
        match="Persisted workspace path is missing or unsafe; cleanup was retained",
    ):
        remove_worktree_workspace(source, workspace_link, repo / ".git")

    assert workspace_link.is_symlink()
    remove_worktree_workspace(source, workspace_link)
    assert not workspace_link.is_symlink()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_remove_worktree_workspace_does_not_git_remove_checkout_symlink(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = cleanup_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    outside = tmp_path / "outside"
    outside.mkdir()
    external_checkout = outside / "checkout"
    run_cleanup_git(
        repo, "worktree", "add", "--detach", external_checkout.as_posix(), "HEAD"
    )
    workspace_path = cache_workspace_path(tmp_path, "workspaces", "run-1", "linked")
    workspace_path.mkdir(parents=True)
    try:
        (workspace_path / "checkout").symlink_to(
            external_checkout,
            target_is_directory=True,
        )
    except OSError:
        pytest.skip("symlink creation is unavailable")

    remove_worktree_workspace(source, workspace_path)

    assert not workspace_path.exists()
    assert external_checkout.exists()
    assert external_checkout.as_posix() in run_cleanup_git(
        repo, "worktree", "list", "--porcelain"
    )


def test_remove_worktree_workspace_retains_registered_missing_checkout(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = cleanup_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    workspace_path = cache_workspace_path(tmp_path, "workspaces", "run-1", "missing")
    checkout_root = workspace_path / "checkout"
    workspace_path.mkdir(parents=True)
    run_cleanup_git(
        repo, "worktree", "add", "--detach", checkout_root.as_posix(), "HEAD"
    )
    shutil.rmtree(checkout_root)

    with pytest.raises(RuntimeError, match="checkout is missing"):
        remove_worktree_workspace(source, workspace_path)

    assert workspace_path.exists()
    assert checkout_root.as_posix() in run_cleanup_git(
        repo, "worktree", "list", "--porcelain"
    )


def test_cleanup_workspace_cache_removes_registered_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = cleanup_git_repo(tmp_path)
    workspace_path = cache_workspace_path(
        tmp_path, "workspaces", "run-1", "node-round1"
    )
    checkout = workspace_path / "checkout"
    workspace_path.mkdir(parents=True)
    run_cleanup_git(repo, "worktree", "add", "--detach", checkout.as_posix(), "HEAD")
    locked_dirs: list[Path] = []

    @contextmanager
    def record_git_metadata_lock(common_git_dir: Path) -> Iterator[None]:
        locked_dirs.append(common_git_dir.resolve(strict=False))
        yield

    monkeypatch.setattr(
        worktree_cleanup,
        "git_metadata_lock",
        record_git_metadata_lock,
    )

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(
            run_key_name="run-1",
            expected_common_git_dir=repo / ".git",
            orphans=True,
        ),
        dry_run=False,
    )

    assert [entry.path for entry in result.entries] == [workspace_path]
    assert not workspace_path.exists()
    assert checkout.as_posix() not in run_cleanup_git(
        repo, "worktree", "list", "--porcelain"
    )
    assert locked_dirs == [(repo / ".git").resolve(strict=False)]


def test_cleanup_workspace_cache_derives_git_dir_for_registered_worktree(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = cleanup_git_repo(tmp_path)
    workspace_path = cache_workspace_path(
        tmp_path, "workspaces", "run-1", "node-round1"
    )
    checkout = workspace_path / "checkout"
    workspace_path.mkdir(parents=True)
    run_cleanup_git(repo, "worktree", "add", "--detach", checkout.as_posix(), "HEAD")

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(run_key_name="run-1", orphans=True),
        dry_run=False,
    )

    assert [entry.path for entry in result.entries] == [workspace_path]
    assert not workspace_path.exists()
    assert checkout.as_posix() not in run_cleanup_git(
        repo, "worktree", "list", "--porcelain"
    )


def test_cleanup_workspace_cache_preserves_registered_worktree_when_git_remove_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = cleanup_git_repo(tmp_path)
    workspace_path = cache_workspace_path(
        tmp_path, "workspaces", "run-1", "node-round1"
    )
    checkout = workspace_path / "checkout"
    workspace_path.mkdir(parents=True)
    run_cleanup_git(repo, "worktree", "add", "--detach", checkout.as_posix(), "HEAD")

    original_run = subprocess.run

    def fail_worktree_remove(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and "worktree" in command and "remove" in command:
            raise subprocess.CalledProcessError(1, command)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_worktree_remove)

    with pytest.raises(subprocess.CalledProcessError):
        cleanup_workspace_cache(
            tmp_path,
            WorkspaceCleanupFilter(
                run_key_name="run-1",
                expected_common_git_dir=repo / ".git",
                orphans=True,
            ),
            dry_run=False,
        )

    assert workspace_path.exists()
    assert checkout.as_posix() in run_cleanup_git(
        repo, "worktree", "list", "--porcelain"
    )
    monkeypatch.undo()
    run_cleanup_git(repo, "worktree", "remove", "--force", checkout.as_posix())


def test_cleanup_workspace_cache_rejects_symlink_git_admin_entry(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.git"
    outside.write_text("gitdir: elsewhere", encoding="utf-8")
    workspace_path = cache_workspace_path(
        tmp_path, "workspaces", "run-1", "node-round1"
    )
    checkout = workspace_path / "checkout"
    checkout.mkdir(parents=True)
    try:
        (checkout / ".git").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(RuntimeError, match="Git metadata could not be verified"):
        cleanup_workspace_cache(
            tmp_path,
            WorkspaceCleanupFilter(run_key_name="run-1", orphans=True),
            dry_run=False,
        )

    assert workspace_path.exists()
    assert (checkout / ".git").is_symlink()
    assert outside.read_text(encoding="utf-8") == "gitdir: elsewhere"


def test_cleanup_workspace_cache_preserves_external_registered_worktree(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    current_root = tmp_path / "current"
    external_root = tmp_path / "external"
    current_root.mkdir()
    external_root.mkdir()
    current_repo = cleanup_git_repo(current_root)
    external_repo = cleanup_git_repo(external_root)
    workspace_path = cache_workspace_path(
        tmp_path, "workspaces", "run-1", "node-round1"
    )
    checkout = workspace_path / "checkout"
    workspace_path.mkdir(parents=True)
    run_cleanup_git(
        external_repo, "worktree", "add", "--detach", checkout.as_posix(), "HEAD"
    )

    with pytest.raises(RuntimeError, match="Git metadata could not be verified"):
        cleanup_workspace_cache(
            tmp_path,
            WorkspaceCleanupFilter(
                run_key_name="run-1",
                expected_common_git_dir=current_repo / ".git",
                orphans=True,
            ),
            dry_run=False,
        )

    assert workspace_path.exists()
    assert checkout.as_posix() in run_cleanup_git(
        external_repo,
        "worktree",
        "list",
        "--porcelain",
    )
    run_cleanup_git(external_repo, "worktree", "prune")


def test_cleanup_workspace_cache_rejects_gitdir_backlink_mismatch_before_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo_parent = tmp_path / "current"
    repo_parent.mkdir()
    repo = cleanup_git_repo(repo_parent)
    workspace_path = cache_workspace_path(
        tmp_path, "workspaces", "run-1", "node-round1"
    )
    checkout = workspace_path / "checkout"
    checkout.mkdir(parents=True)
    admin_git_dir = repo / ".git" / "worktrees" / "forged"
    admin_git_dir.mkdir(parents=True)
    (admin_git_dir / "gitdir").write_text(
        (tmp_path / "other-checkout" / ".git").as_posix(),
        encoding="utf-8",
    )
    (checkout / ".git").write_text(
        f"gitdir: {admin_git_dir.as_posix()}\n",
        encoding="utf-8",
    )
    worktree_commands: list[list[str]] = []
    original_run = subprocess.run

    def record_worktree_commands(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and "worktree" in command:
            worktree_commands.append(command)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(workspace_git.subprocess, "run", record_worktree_commands)

    with pytest.raises(RuntimeError, match="Git metadata could not be verified"):
        cleanup_workspace_cache(
            tmp_path,
            WorkspaceCleanupFilter(
                run_key_name="run-1",
                expected_common_git_dir=repo / ".git",
                orphans=True,
            ),
            dry_run=False,
        )

    assert workspace_path.exists()
    assert worktree_commands == []
