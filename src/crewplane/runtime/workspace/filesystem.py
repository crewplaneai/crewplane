from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)
from crewplane.core.workspace.cache import workspace_cache_root


def workspace_run_hierarchy(
    plan: PreflightExecutionPlan,
    source: WorkspaceSourceSnapshot,
    family: str,
) -> tuple[Path, Path, Path, Path]:
    """Return cache, family, repository, and run paths without creating them."""
    cache_root = workspace_cache_root(runtime_workspace_cache_root(plan))
    family_root = cache_root / family
    repository_root = family_root / source.repository_id
    run_root = repository_root / plan.run_key_name
    return cache_root, family_root, repository_root, run_root


def workspace_run_root(
    plan: PreflightExecutionPlan,
    source: WorkspaceSourceSnapshot,
    family: str,
) -> Path:
    hierarchy = workspace_run_hierarchy(plan, source, family)
    for directory in hierarchy:
        ensure_owner_private_dir(directory)
    return hierarchy[-1]


def runtime_workspace_cache_root(plan: PreflightExecutionPlan) -> str | None:
    workspace = plan.runtime_config_snapshot.get("workspace")
    if not isinstance(workspace, dict):
        return None
    value = workspace.get("cache_root")
    return value if isinstance(value, str) else None


def ensure_owner_private_dir(path: Path) -> None:
    if path.exists() and not path.is_dir():
        raise RuntimeError(
            f"Workspace cache path is not a directory: {path.as_posix()}"
        )
    if path.is_symlink():
        raise RuntimeError(
            f"Workspace cache path must not be a symlink: {path.as_posix()}"
        )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def remove_workspace_path(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        path.unlink(missing_ok=True)
        return
    for current_root, dir_names, file_names in os.walk(path, topdown=False):
        del file_names
        current = Path(current_root)
        for dir_name in dir_names:
            dir_path = current / dir_name
            if not dir_path.is_symlink():
                dir_path.chmod(0o700)
        current.chmod(0o700)
    shutil.rmtree(path)
