from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import crewplane.runtime.workspace.git as workspace_git
from crewplane.runtime.workspace.cleanup import (
    WorkspaceCleanupFilter,
    cleanup_workspace_cache,
    parse_duration_seconds,
)
from crewplane.runtime.workspace.snapshot import remove_workspace_path
from crewplane.runtime.workspace.worktree import cleanup as worktree_cleanup
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from crewplane.runtime.workspace.worktree.ref_cleanup import (
    cleanup_plan_workspace_refs,
    delete_run_workspace_refs,
)
from tests.helpers.workspace_service import workspace_plan


def test_cleanup_workspace_cache_dry_run_preserves_paths(tmp_path: Path) -> None:
    workspace_path = _workspace_path(tmp_path, "workspaces", "run-1", "node-round1")
    workspace_path.mkdir(parents=True)
    (workspace_path / "file.txt").write_text("payload", encoding="utf-8")

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(run_key_name="run-1"),
        dry_run=True,
    )

    assert len(result.entries) == 1
    assert result.entries[0].removed is False
    assert result.entries[0].size_bytes == len("payload")
    assert workspace_path.exists()


def test_worktree_disk_usage_does_not_follow_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-payload", encoding="utf-8")
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    link_path = workspace_path / "link.txt"
    try:
        link_path.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    assert (
        worktree_cleanup.worktree_disk_usage(workspace_path)
        == link_path.lstat().st_size
    )


def test_cleanup_workspace_cache_ignores_symlink_candidates(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    workspace_link = _workspace_path(tmp_path, "workspaces", "run-1", "linked")
    workspace_link.parent.mkdir(parents=True)
    try:
        workspace_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(run_key_name="run-1"),
        dry_run=False,
    )

    assert result.entries == ()
    assert workspace_link.is_symlink()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_remove_workspace_path_unlinks_top_level_symlink_without_chmod_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    workspace_link = tmp_path / "workspace"
    try:
        workspace_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    chmod_targets: list[Path] = []

    def record_chmod(self: Path, mode: int) -> None:
        del mode
        chmod_targets.append(self)

    monkeypatch.setattr(Path, "chmod", record_chmod)

    remove_workspace_path(workspace_link)

    assert not workspace_link.exists()
    assert not workspace_link.is_symlink()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert chmod_targets == []


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
    repo = _git_repo(tmp_path)
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
    _git(repo, "worktree", "add", "--detach", external_checkout.as_posix(), "HEAD")
    workspace_link = _workspace_path(tmp_path, "workspaces", "run-1", "linked")
    workspace_link.parent.mkdir(parents=True)
    try:
        workspace_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    remove_worktree_workspace(source, workspace_link)

    assert not workspace_link.exists()
    assert not workspace_link.is_symlink()
    assert external_checkout.exists()
    assert external_checkout.as_posix() in _git(repo, "worktree", "list", "--porcelain")


def test_remove_worktree_workspace_does_not_git_remove_checkout_symlink(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = _git_repo(tmp_path)
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
    _git(repo, "worktree", "add", "--detach", external_checkout.as_posix(), "HEAD")
    workspace_path = _workspace_path(tmp_path, "workspaces", "run-1", "linked")
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
    assert external_checkout.as_posix() in _git(repo, "worktree", "list", "--porcelain")


def test_remove_worktree_workspace_prunes_registered_missing_checkout(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = _git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    workspace_path = _workspace_path(tmp_path, "workspaces", "run-1", "missing")
    checkout_root = workspace_path / "checkout"
    workspace_path.mkdir(parents=True)
    _git(repo, "worktree", "add", "--detach", checkout_root.as_posix(), "HEAD")
    shutil.rmtree(checkout_root)

    remove_worktree_workspace(source, workspace_path)

    assert not workspace_path.exists()
    assert checkout_root.as_posix() not in _git(repo, "worktree", "list", "--porcelain")


def test_cleanup_workspace_cache_removes_matching_paths(tmp_path: Path) -> None:
    old_workspace = _workspace_path(tmp_path, "snapshots", "run-1", "old")
    new_workspace = _workspace_path(tmp_path, "snapshots", "run-1", "new")
    old_workspace.mkdir(parents=True)
    new_workspace.mkdir()
    old_time = time.time() - 7200
    os.utime(old_workspace, (old_time, old_time))

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(run_key_name="run-1", older_than_seconds=3600),
        dry_run=False,
    )

    assert [entry.path for entry in result.entries] == [old_workspace]
    assert result.entries[0].removed is True
    assert not old_workspace.exists()
    assert new_workspace.exists()


def test_cleanup_workspace_cache_propagates_remove_errors_without_ref_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_path = _workspace_path(tmp_path, "snapshots", "run-1", "node-round1")
    workspace_path.mkdir(parents=True)
    removed_runs: list[str] = []

    def fail_rmtree(path: Path) -> None:
        raise OSError(f"cannot remove {path}")

    monkeypatch.setattr(worktree_cleanup.shutil, "rmtree", fail_rmtree)

    with pytest.raises(OSError, match="cannot remove"):
        cleanup_workspace_cache(
            tmp_path,
            WorkspaceCleanupFilter(run_key_name="run-1"),
            dry_run=False,
            ref_cleanup=lambda run_key: removed_runs.append(run_key) or 1,
        )

    assert workspace_path.exists()
    assert removed_runs == []


def test_cleanup_workspace_cache_removes_registered_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = _git_repo(tmp_path)
    workspace_path = _workspace_path(tmp_path, "workspaces", "run-1", "node-round1")
    checkout = workspace_path / "checkout"
    workspace_path.mkdir(parents=True)
    _git(repo, "worktree", "add", "--detach", checkout.as_posix(), "HEAD")
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
        ),
        dry_run=False,
    )

    assert [entry.path for entry in result.entries] == [workspace_path]
    assert not workspace_path.exists()
    assert checkout.as_posix() not in _git(repo, "worktree", "list", "--porcelain")
    assert locked_dirs == [(repo / ".git").resolve(strict=False)]


def test_cleanup_workspace_cache_derives_git_dir_for_registered_worktree(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = _git_repo(tmp_path)
    workspace_path = _workspace_path(tmp_path, "workspaces", "run-1", "node-round1")
    checkout = workspace_path / "checkout"
    workspace_path.mkdir(parents=True)
    _git(repo, "worktree", "add", "--detach", checkout.as_posix(), "HEAD")

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(run_key_name="run-1"),
        dry_run=False,
    )

    assert [entry.path for entry in result.entries] == [workspace_path]
    assert not workspace_path.exists()
    assert checkout.as_posix() not in _git(repo, "worktree", "list", "--porcelain")


def test_cleanup_workspace_cache_preserves_registered_worktree_when_git_remove_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = _git_repo(tmp_path)
    workspace_path = _workspace_path(tmp_path, "workspaces", "run-1", "node-round1")
    checkout = workspace_path / "checkout"
    workspace_path.mkdir(parents=True)
    _git(repo, "worktree", "add", "--detach", checkout.as_posix(), "HEAD")

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
            ),
            dry_run=False,
        )

    assert workspace_path.exists()
    assert checkout.as_posix() in _git(repo, "worktree", "list", "--porcelain")
    monkeypatch.undo()
    _git(repo, "worktree", "remove", "--force", checkout.as_posix())


def test_cleanup_workspace_cache_rejects_symlink_git_admin_entry(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.git"
    outside.write_text("gitdir: elsewhere", encoding="utf-8")
    workspace_path = _workspace_path(tmp_path, "workspaces", "run-1", "node-round1")
    checkout = workspace_path / "checkout"
    checkout.mkdir(parents=True)
    try:
        (checkout / ".git").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(RuntimeError, match="Git metadata could not be verified"):
        cleanup_workspace_cache(
            tmp_path,
            WorkspaceCleanupFilter(run_key_name="run-1"),
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
    current_repo = _git_repo(current_root)
    external_repo = _git_repo(external_root)
    workspace_path = _workspace_path(tmp_path, "workspaces", "run-1", "node-round1")
    checkout = workspace_path / "checkout"
    workspace_path.mkdir(parents=True)
    _git(external_repo, "worktree", "add", "--detach", checkout.as_posix(), "HEAD")

    with pytest.raises(RuntimeError, match="Git metadata could not be verified"):
        cleanup_workspace_cache(
            tmp_path,
            WorkspaceCleanupFilter(
                run_key_name="run-1",
                expected_common_git_dir=current_repo / ".git",
            ),
            dry_run=False,
        )

    assert workspace_path.exists()
    assert checkout.as_posix() in _git(
        external_repo,
        "worktree",
        "list",
        "--porcelain",
    )
    _git(external_repo, "worktree", "prune")


def test_cleanup_workspace_cache_rejects_gitdir_backlink_mismatch_before_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo_parent = tmp_path / "current"
    repo_parent.mkdir()
    repo = _git_repo(repo_parent)
    workspace_path = _workspace_path(tmp_path, "workspaces", "run-1", "node-round1")
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
            ),
            dry_run=False,
        )

    assert workspace_path.exists()
    assert worktree_commands == []


def test_cleanup_workspace_cache_filters_by_state_status(tmp_path: Path) -> None:
    succeeded = _workspace_path(tmp_path, "workspaces", "run-1", "succeeded")
    failed = _workspace_path(tmp_path, "snapshots", "run-1", "failed")
    orphan = _review_workspace_path(tmp_path, "run-1", "review", "orphan")
    succeeded.mkdir(parents=True)
    failed.mkdir(parents=True)
    orphan.mkdir(parents=True)
    statuses = {
        ("run-1", "succeeded"): "succeeded",
        ("run-1", "failed"): "failed",
    }

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(
            run_key_name="run-1",
            statuses=frozenset({"failed"}),
            orphans=True,
        ),
        dry_run=True,
        status_lookup=lambda run_key, cache_key: statuses.get((run_key, cache_key)),
    )

    entries_by_name = {entry.path.name: entry for entry in result.entries}
    assert set(entries_by_name) == {"failed", "orphan"}
    assert entries_by_name["failed"].status == "failed"
    assert entries_by_name["failed"].orphan is False
    assert entries_by_name["orphan"].status is None
    assert entries_by_name["orphan"].orphan is True


def test_cleanup_workspace_cache_deletes_refs_once_per_removed_run(
    tmp_path: Path,
) -> None:
    first = _workspace_path(tmp_path, "workspaces", "run-1", "first")
    second = _workspace_path(tmp_path, "snapshots", "run-1", "second")
    other = _workspace_path(tmp_path, "workspaces", "run-2", "other")
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    other.mkdir(parents=True)
    deleted_runs: list[str] = []

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(run_key_name="run-1"),
        dry_run=False,
        ref_cleanup=lambda run_key: deleted_runs.append(run_key) or 2,
    )

    assert {entry.path for entry in result.entries} == {first, second}
    assert deleted_runs == ["run-1"]
    assert result.removed_ref_count == 2


def test_cleanup_workspace_cache_filters_current_repository_by_default(
    tmp_path: Path,
) -> None:
    current = _workspace_path(tmp_path, "workspaces", "run-1", "current")
    other = tmp_path / "workspaces" / "repo-2" / "run-1" / "other"
    current.mkdir(parents=True)
    other.mkdir(parents=True)

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(run_key_name="run-1", repository_id="repo-1"),
        dry_run=False,
    )

    assert [entry.path for entry in result.entries] == [current]
    assert not current.exists()
    assert other.exists()


def test_delete_run_workspace_refs_removes_only_selected_run_refs(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = _git_repo(tmp_path)
    common_git_dir = repo / ".git"
    _git(repo, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD")
    _git(repo, "update-ref", "refs/crewplane/runs/run-1/node/b", "HEAD")
    _git(repo, "update-ref", "refs/crewplane/runs/run-2/node/a", "HEAD")

    removed = delete_run_workspace_refs(repo, common_git_dir, "run-1")

    assert removed == 2
    assert _git(repo, "for-each-ref", "refs/crewplane/runs/run-1") == ""
    assert "refs/crewplane/runs/run-2/node/a" in _git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/crewplane/runs/run-2",
    )


def test_cleanup_plan_workspace_refs_respects_cleanup_on_success(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = _git_repo(tmp_path)
    _git(
        repo,
        "update-ref",
        "refs/crewplane/runs/workspace-run-001/node/a",
        "HEAD",
    )
    retained_plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )

    assert cleanup_plan_workspace_refs(retained_plan) == 0
    assert "refs/crewplane/runs/workspace-run-001/node/a" in _git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/crewplane/runs/workspace-run-001",
    )

    cleanup_plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )

    assert cleanup_plan_workspace_refs(cleanup_plan) == 1
    assert _git(repo, "for-each-ref", "refs/crewplane/runs/workspace-run-001") == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("30", 30), ("2m", 120), ("3h", 10800), ("4d", 345600)],
)
def test_parse_duration_seconds(raw: str, expected: int) -> None:
    assert parse_duration_seconds(raw) == expected


def test_parse_duration_seconds_rejects_invalid_value() -> None:
    with pytest.raises(ValueError, match="Duration"):
        parse_duration_seconds("soon")


def _workspace_path(tmp_path: Path, family: str, run_key: str, name: str) -> Path:
    return tmp_path / family / "repo-1" / run_key / name


def _review_workspace_path(
    tmp_path: Path,
    run_key: str,
    node_slug: str,
    name: str,
) -> Path:
    return tmp_path / "review-workspaces" / "repo-1" / run_key / node_slug / name


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Crewplane Test")
    _git(repo, "config", "user.email", "crewplane-test@example.invalid")
    (repo / "README.md").write_text("ready\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")
