from __future__ import annotations

import subprocess
from pathlib import Path

from crewplane.core.preflight.models import WorkspaceSourceSnapshot

from ..git import git, git_error
from ..locks import git_metadata_lock
from .lineage import ensure_source_commit_available
from .policy import (
    active_git_dir,
    reject_common_git_policy_drift,
    reject_worktree_git_policy_drift,
)
from .protected_refs import (
    ProtectedRefSnapshot,
    protected_ref_snapshot,
    protected_ref_snapshot_for_scopes,
)
from .refs import checked_ref
from .reset import reset_reusable_worktree_checkout
from .types import WorktreeSourceRef, WorktreeWorkspace


def reuse_worktree_workspace(
    workspace_path: Path,
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    expected_git_dir: Path,
    protected_ref_scopes: tuple[str, ...] | None = None,
) -> WorktreeWorkspace:
    checkout_root = workspace_path / "checkout"
    try:
        ensure_source_commit_available(source, source_ref)
        with git_metadata_lock(Path(source.common_git_dir)):
            reset_reusable_worktree_checkout(
                checkout_root,
                source_ref.source_commit,
                Path(source.git_top_level),
                Path(source.common_git_dir),
                expected_git_dir,
            )
        cwd = checkout_root / source.project_root_relative_path
        if source.project_root_relative_path == ".":
            cwd = checkout_root
        _verify_reused_worktree_ready(source, checkout_root, source_ref)
        protected_refs = _protected_ref_snapshot(source, protected_ref_scopes)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Workspace worktree reuse failed: {git_error(exc)}"
        ) from exc
    return WorktreeWorkspace(
        workspace_path=workspace_path,
        checkout_root=checkout_root,
        cwd=cwd,
        git_dir=expected_git_dir,
        source_ref=source_ref,
        protected_refs=protected_refs,
        lock_mode="reused_incremental_reset",
    )


def _verify_reused_worktree_ready(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
    source_ref: WorktreeSourceRef,
) -> None:
    command = git(checkout_root)
    head = command.text("rev-parse", "HEAD^{commit}")
    if head != source_ref.source_commit:
        raise RuntimeError("Reused workspace worktree HEAD does not match source.")
    if command.text("rev-parse", "--is-inside-work-tree") != "true":
        raise RuntimeError("Reused workspace worktree is not registered.")
    git_dir = active_git_dir(checkout_root)
    if not git_dir.is_relative_to(Path(source.common_git_dir)):
        raise RuntimeError("Reused workspace Git dir escapes the common Git dir.")
    reject_common_git_policy_drift(
        Path(source.git_top_level),
        Path(source.common_git_dir),
    )
    reject_worktree_git_policy_drift(checkout_root)


def _protected_ref_snapshot(
    source: WorkspaceSourceSnapshot,
    protected_ref_scopes: tuple[str, ...] | None,
) -> ProtectedRefSnapshot:
    repo_root = Path(source.git_top_level)
    if protected_ref_scopes is None:
        return protected_ref_snapshot(repo_root)
    checked_scopes = tuple(
        checked_ref(repo_root, scope) for scope in protected_ref_scopes
    )
    return protected_ref_snapshot_for_scopes(repo_root, checked_scopes)
