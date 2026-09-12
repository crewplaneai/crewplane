from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import crewplane.runtime.workspace.git as workspace_git
from crewplane.runtime.workspace.cleanup import (
    WorkspaceCleanupFilter,
    cleanup_candidates,
    cleanup_candidates_for_repository,
    cleanup_workspace_cache,
    parse_duration_seconds,
)
from crewplane.runtime.workspace.filesystem import (
    remove_workspace_path,
)
from crewplane.runtime.workspace.worktree import cleanup as worktree_cleanup
from crewplane.runtime.workspace.worktree.ref_cleanup import (
    cleanup_plan_workspace_refs,
    delete_run_workspace_refs,
)
from tests.helpers.workspace_service import workspace_plan
from tests.unit.runtime.workspace.cleanup_support import (
    cache_workspace_path,
    cleanup_git_repo,
    run_cleanup_git,
)


@pytest.mark.parametrize("name", ['cache"quoted', r"cache\literal"])
def test_nul_worktree_paths_accept_printable_literal_path_characters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    checkout = tmp_path / name
    output = (f"worktree {checkout.as_posix()}\0HEAD {'a' * 40}\0detached\0\0").encode()

    def return_worktree_records(*args, **kwargs) -> subprocess.CompletedProcess[bytes]:
        command = args[0] if args else kwargs["args"]
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(workspace_git.subprocess, "run", return_worktree_records)

    assert worktree_cleanup.registered_worktree_paths(tmp_path / ".git") == (
        checkout.resolve(strict=False),
    )


@pytest.mark.parametrize("name", ['cache"quoted', r"cache\literal"])
def test_legacy_worktree_paths_remain_strict_for_quoted_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    checkout = tmp_path / name
    output = (
        f"worktree {json.dumps(checkout.as_posix())}\nHEAD {'a' * 40}\ndetached\n\n"
    ).encode()

    def return_legacy_worktree_records(
        *args, **kwargs
    ) -> subprocess.CompletedProcess[bytes]:
        command = args[0] if args else kwargs["args"]
        if "-z" in command:
            raise subprocess.CalledProcessError(129, command)
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(
        workspace_git.subprocess,
        "run",
        return_legacy_worktree_records,
    )

    with pytest.raises(RuntimeError, match="quoted, escaped, or malformed"):
        worktree_cleanup.registered_worktree_paths(tmp_path / ".git")


def test_cleanup_workspace_cache_dry_run_preserves_paths(tmp_path: Path) -> None:
    workspace_path = cache_workspace_path(
        tmp_path, "workspaces", "run-1", "node-round1"
    )
    workspace_path.mkdir(parents=True)
    (workspace_path / "file.txt").write_text("payload", encoding="utf-8")

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(run_key_name="run-1", orphans=True),
        dry_run=True,
    )

    assert len(result.entries) == 1
    assert result.entries[0].removed is False
    assert result.entries[0].size_bytes == len("payload")
    assert workspace_path.exists()


def test_cleanup_candidates_preserve_fixed_depth_order_and_exclude_links(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    expected = (
        ("run-z", "review-workspaces/repo-a/run-z/node-a/reviewer"),
        ("run-a", "review-workspaces/repo-b/run-a/node-b/reviewer"),
        ("run-z", "snapshots/repo-a/run-z/executor"),
        ("run-a", "workspace-runs/run-a/legacy"),
        ("run-z", "workspaces/repo-a/run-z/a"),
        ("run-z", "workspaces/repo-a/run-z/z"),
        ("run-a", "workspaces/repo-b/run-a/executor"),
    )
    for run_key, relative in reversed(expected):
        del run_key
        path = cache / relative
        (path / "nested" / "not-a-candidate").mkdir(parents=True)
        for ancestor in (path, *path.parents):
            if ancestor == cache:
                break
            link = ancestor.with_name(ancestor.name + "-link")
            if not link.is_symlink():
                link.symlink_to(ancestor, target_is_directory=True)
        (path.parent / "not-a-directory").write_text("ignored")
    (cache / "snapshots" / "repo-empty").mkdir()

    assert cleanup_candidates(cache) == tuple(
        (run_key, cache / relative) for run_key, relative in expected
    )
    assert cleanup_candidates_for_repository(cache, "repo-a") == tuple(
        (run_key, cache / relative)
        for run_key, relative in expected
        if "/repo-a/" in relative
    )
    assert cleanup_candidates_for_repository(cache, "missing") == ()
    assert cleanup_candidates_for_repository(cache, "repo-a-link") == ()
    assert cleanup_candidates(tmp_path / "missing") == ()
    for family in ("workspace-runs", "workspaces", "snapshots", "review-workspaces"):
        family_path = cache / family
        outside = tmp_path / family
        family_path.rename(outside)
        family_path.symlink_to(outside, target_is_directory=True)
        assert cleanup_candidates(cache) == tuple(
            (run_key, cache / relative)
            for run_key, relative in expected
            if not relative.startswith(f"{family}/")
        )
        # Filtered discovery starts at the repository, below the family directory.
        assert cleanup_candidates_for_repository(cache, "repo-a") == tuple(
            (run_key, cache / relative)
            for run_key, relative in expected
            if "/repo-a/" in relative
        )
        family_path.unlink()
        outside.rename(family_path)


def test_cleanup_candidates_propagate_directory_enumeration_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "workspaces").mkdir()

    def deny_listing(path: Path):
        raise PermissionError(path)

    monkeypatch.setattr(Path, "iterdir", deny_listing)
    with pytest.raises(PermissionError):
        cleanup_candidates(tmp_path)


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


def test_claimed_worktree_cleanup_does_not_fall_through_after_git_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_path = tmp_path / "workspace"
    checkout_root = workspace_path / "checkout"
    checkout_root.mkdir(parents=True)
    common_git_dir = tmp_path / "repo.git"
    worktree_git_dir = common_git_dir / "worktrees" / "claimed"
    (checkout_root / ".git").write_text(
        f"gitdir: {worktree_git_dir.as_posix()}\n",
        encoding="utf-8",
    )
    git_file = checkout_root / ".git"
    git_probe_count = 0
    original_lstat = Path.lstat

    def remove_git_file_between_probes(path: Path) -> os.stat_result:
        nonlocal git_probe_count
        if path == git_file:
            git_probe_count += 1
            if git_probe_count == 2:
                git_file.unlink()
                raise FileNotFoundError(git_file)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", remove_git_file_between_probes)

    with pytest.raises(RuntimeError):
        worktree_cleanup.remove_unknown_workspace_path(
            workspace_path,
            common_git_dir,
            worktree_git_dir,
        )

    assert git_probe_count == 2
    assert workspace_path.exists()


def test_cleanup_workspace_cache_ignores_symlink_candidates(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    workspace_link = cache_workspace_path(tmp_path, "workspaces", "run-1", "linked")
    workspace_link.parent.mkdir(parents=True)
    try:
        workspace_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(run_key_name="run-1", orphans=True),
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


def test_cleanup_workspace_cache_removes_matching_paths(tmp_path: Path) -> None:
    old_workspace = cache_workspace_path(tmp_path, "snapshots", "run-1", "old")
    new_workspace = cache_workspace_path(tmp_path, "snapshots", "run-1", "new")
    old_workspace.mkdir(parents=True)
    new_workspace.mkdir()
    old_time = time.time() - 7200
    os.utime(old_workspace, (old_time, old_time))

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(
            run_key_name="run-1", older_than_seconds=3600, orphans=True
        ),
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
    workspace_path = cache_workspace_path(tmp_path, "snapshots", "run-1", "node-round1")
    workspace_path.mkdir(parents=True)
    removed_runs: list[str] = []

    def fail_rmtree(path: Path) -> None:
        raise OSError(f"cannot remove {path}")

    monkeypatch.setattr(worktree_cleanup.shutil, "rmtree", fail_rmtree)

    with pytest.raises(OSError, match="cannot remove"):
        cleanup_workspace_cache(
            tmp_path,
            WorkspaceCleanupFilter(run_key_name="run-1", orphans=True),
            dry_run=False,
            ref_cleanup=lambda run_key: removed_runs.append(run_key) or 1,
        )

    assert workspace_path.exists()
    assert removed_runs == []


def test_cleanup_workspace_cache_filters_by_state_status(tmp_path: Path) -> None:
    succeeded = cache_workspace_path(tmp_path, "workspaces", "run-1", "succeeded")
    failed = cache_workspace_path(tmp_path, "snapshots", "run-1", "failed")
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


def test_cleanup_workspace_cache_global_default_selects_unknown_status(
    tmp_path: Path,
) -> None:
    workspace_path = cache_workspace_path(tmp_path, "workspaces", "run-1", "unknown")
    workspace_path.mkdir(parents=True)

    def unknown_status(run_key: str, cache_key: str) -> str:
        del run_key, cache_key
        return "unknown"

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(),
        dry_run=True,
        status_lookup=unknown_status,
    )

    assert [entry.path for entry in result.entries] == [workspace_path]
    assert result.entries[0].status == "unknown"


def test_cleanup_workspace_cache_deletes_refs_once_per_removed_run(
    tmp_path: Path,
) -> None:
    first = cache_workspace_path(tmp_path, "workspaces", "run-1", "first")
    second = cache_workspace_path(tmp_path, "snapshots", "run-1", "second")
    other = cache_workspace_path(tmp_path, "workspaces", "run-2", "other")
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    other.mkdir(parents=True)
    deleted_runs: list[str] = []

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(run_key_name="run-1", orphans=True),
        dry_run=False,
        ref_cleanup=lambda run_key: deleted_runs.append(run_key) or 2,
    )

    assert {entry.path for entry in result.entries} == {first, second}
    assert deleted_runs == ["run-1"]
    assert result.removed_ref_count == 2


def test_cleanup_workspace_cache_filters_current_repository_by_default(
    tmp_path: Path,
) -> None:
    current = cache_workspace_path(tmp_path, "workspaces", "run-1", "current")
    other = tmp_path / "workspaces" / "repo-2" / "run-1" / "other"
    current.mkdir(parents=True)
    other.mkdir(parents=True)

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(
            run_key_name="run-1", repository_id="repo-1", orphans=True
        ),
        dry_run=False,
    )

    assert [entry.path for entry in result.entries] == [current]
    assert not current.exists()
    assert other.exists()


def test_delete_run_workspace_refs_preserves_unrecorded_run_refs(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = cleanup_git_repo(tmp_path)
    common_git_dir = repo / ".git"
    run_cleanup_git(repo, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD")
    run_cleanup_git(repo, "update-ref", "refs/crewplane/runs/run-1/node/b", "HEAD")
    run_cleanup_git(repo, "update-ref", "refs/crewplane/runs/run-2/node/a", "HEAD")

    removed = delete_run_workspace_refs(repo, common_git_dir, repo, "run-1")

    assert removed == 0
    assert "refs/crewplane/runs/run-1/node/a" in run_cleanup_git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/crewplane/runs/run-1",
    )
    assert "refs/crewplane/runs/run-2/node/a" in run_cleanup_git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/crewplane/runs/run-2",
    )


def test_cleanup_plan_workspace_refs_does_not_delete_unrecorded_refs(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = cleanup_git_repo(tmp_path)
    run_cleanup_git(
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
    assert "refs/crewplane/runs/workspace-run-001/node/a" in run_cleanup_git(
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

    assert cleanup_plan_workspace_refs(cleanup_plan) == 0
    assert "refs/crewplane/runs/workspace-run-001/node/a" in run_cleanup_git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/crewplane/runs/workspace-run-001",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("30", 30), ("2m", 120), ("3h", 10800), ("4d", 345600)],
)
def test_parse_duration_seconds(raw: str, expected: int) -> None:
    assert parse_duration_seconds(raw) == expected


def test_parse_duration_seconds_rejects_invalid_value() -> None:
    with pytest.raises(ValueError, match="Duration"):
        parse_duration_seconds("soon")


def _review_workspace_path(
    tmp_path: Path,
    run_key: str,
    node_slug: str,
    name: str,
) -> Path:
    return tmp_path / "review-workspaces" / "repo-1" / run_key / node_slug / name
