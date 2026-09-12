from __future__ import annotations

from pathlib import Path
from typing import Literal

from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)
from crewplane.core.workspace.naming import safe_file_component

from ..filesystem import (
    ensure_owner_private_dir,
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
    if parent_slug is not None:
        run_root = run_root / safe_file_component(parent_slug)
        ensure_owner_private_dir(run_root)
    workspace_path = run_root / slug
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
