from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from crewplane.core.preflight.models import WorkspaceSourceSnapshot

from ..filesystem import (
    remove_workspace_path,
)
from ..git import git
from ..locks import git_metadata_lock
from .checkout_identity import (
    require_regular_worktree_git_file,
    verify_worktree_git_metadata_identity,
    worktree_is_registered,
)
from .head import detach_attached_head_for_disposal
from .policy import active_git_dir


def remove_claimed_worktree_workspace(
    source: WorkspaceSourceSnapshot,
    workspace_path: Path,
    expected_git_dir: Path,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    if workspace_path.is_symlink() or not workspace_path.is_dir():
        raise RuntimeError(
            "Persisted workspace path is missing or unsafe; cleanup was retained."
        )
    checkout_root = workspace_path / "checkout"
    if checkout_root.is_symlink() or not checkout_root.is_dir():
        raise RuntimeError(
            "Persisted workspace checkout is missing or unsafe; cleanup was retained."
        )
    _remove_worktree_checkout(
        source,
        workspace_path,
        checkout_root,
        expected_git_dir,
        cancel_requested,
    )


def remove_unclaimed_worktree_workspace(
    source: WorkspaceSourceSnapshot,
    workspace_path: Path,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    if workspace_path.is_symlink() or not workspace_path.is_dir():
        remove_workspace_path(workspace_path)
        return
    checkout_root = workspace_path / "checkout"
    if checkout_root.is_symlink():
        remove_workspace_path(workspace_path)
        return
    _remove_worktree_checkout(
        source,
        workspace_path,
        checkout_root,
        None,
        cancel_requested,
    )


def _remove_worktree_checkout(
    source: WorkspaceSourceSnapshot,
    workspace_path: Path,
    checkout_root: Path,
    expected_git_dir: Path | None,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    with git_metadata_lock(Path(source.common_git_dir), cancel_requested):
        if worktree_is_registered(source, checkout_root):
            _remove_registered_worktree(source, checkout_root, expected_git_dir)
        else:
            _reject_unsafe_unregistered_checkout(checkout_root, expected_git_dir)
    remove_workspace_path(workspace_path)


def _remove_registered_worktree(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
    expected_git_dir: Path | None,
) -> None:
    if not checkout_root.is_dir():
        raise RuntimeError(
            "Registered workspace checkout is missing; cleanup was retained."
        )
    git_file = require_regular_worktree_git_file(checkout_root)
    git_dir = active_git_dir(checkout_root)
    _verify_cleanup_git_dir(source, git_file, git_dir, expected_git_dir)
    detach_attached_head_for_disposal(checkout_root)
    git(Path(source.git_top_level)).run(
        "worktree",
        "remove",
        "--force",
        "--force",
        checkout_root.as_posix(),
    )


def _verify_cleanup_git_dir(
    source: WorkspaceSourceSnapshot,
    git_file: Path,
    git_dir: Path,
    expected_git_dir: Path | None,
) -> None:
    if expected_git_dir is not None and git_dir != expected_git_dir.resolve(
        strict=False
    ):
        raise RuntimeError(
            "Registered workspace Git dir does not match materialized identity; "
            "cleanup was retained."
        )
    if not git_dir.is_relative_to(Path(source.common_git_dir).resolve(strict=False)):
        raise RuntimeError(
            "Registered workspace Git dir escapes the common Git dir; cleanup was "
            "retained."
        )
    verify_worktree_git_metadata_identity(git_file, git_dir)


def _reject_unsafe_unregistered_checkout(
    checkout_root: Path,
    expected_git_dir: Path | None,
) -> None:
    git_entry = checkout_root / ".git"
    if expected_git_dir is not None or git_entry.exists() or git_entry.is_symlink():
        raise RuntimeError(
            "Workspace checkout is not registered; cleanup was retained."
        )
