from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Literal

from .git_policy import portable_path_key


def workspace_cache_root(cache_root: str | None) -> Path:
    if cache_root:
        return Path(cache_root).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "crewplane"
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "crewplane"


def workspace_cache_forbidden_roots(
    project_root: Path,
    state_dir: Path,
    active_git_dir: Path | None = None,
    common_git_dir: Path | None = None,
) -> tuple[Path, ...]:
    roots = (
        project_root,
        state_dir,
        state_dir / "execution-stages",
        state_dir / "execution-results",
        state_dir / "locks",
        active_git_dir,
        common_git_dir,
    )
    return tuple(root for root in roots if root is not None)


def workspace_cache_root_failure(
    cache_root: Path,
    project_root: Path,
    state_dir: Path,
    active_git_dir: Path | None = None,
    common_git_dir: Path | None = None,
) -> Literal["relative", "symlink", "overlap"] | None:
    """Check absolute path, symlink, then overlap constraints in order."""

    if not cache_root.is_absolute():
        return "relative"
    if cache_root.is_symlink():
        return "symlink"
    blocked_roots = workspace_cache_forbidden_roots(
        project_root, state_dir, active_git_dir, common_git_dir
    )
    if any(paths_overlap(cache_root, blocked) for blocked in blocked_roots):
        return "overlap"
    return None


def paths_overlap(left: Path, right: Path) -> bool:
    resolved_left = left.expanduser().resolve(strict=False)
    resolved_right = right.expanduser().resolve(strict=False)
    case_left = Path(portable_path_key(str(resolved_left)))
    case_right = Path(portable_path_key(str(resolved_right)))
    return (
        resolved_left == resolved_right
        or resolved_left.is_relative_to(resolved_right)
        or resolved_right.is_relative_to(resolved_left)
        or case_left == case_right
        or case_left.is_relative_to(case_right)
        or case_right.is_relative_to(case_left)
    )
