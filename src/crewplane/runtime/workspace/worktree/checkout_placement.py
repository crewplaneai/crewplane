from __future__ import annotations

from pathlib import Path
from typing import Literal

from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)

from ..filesystem import (
    ensure_owner_private_dir,
    workspace_invocation_path,
    workspace_run_root,
)


def allocate_worktree_workspace(
    plan: PreflightExecutionPlan,
    slug: str,
    source: WorkspaceSourceSnapshot,
    workspace_family: Literal["workspaces", "review-workspaces"],
    parent_slug: str | None,
) -> tuple[Path, Path]:
    run_root = workspace_run_root(plan, source, workspace_family)
    workspace_path = workspace_invocation_path(run_root, slug, parent_slug)
    if parent_slug is not None:
        ensure_owner_private_dir(workspace_path.parent)
    if workspace_path.exists() or workspace_path.is_symlink():
        raise RuntimeError(
            f"Workspace path already exists: {workspace_path.as_posix()}"
        )
    workspace_path.mkdir(mode=0o700)
    workspace_path.chmod(0o700)
    return workspace_path, workspace_path / "checkout"


def worktree_project_cwd(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
) -> Path:
    if source.project_root_relative_path == ".":
        return checkout_root
    cwd = checkout_root / source.project_root_relative_path
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd
