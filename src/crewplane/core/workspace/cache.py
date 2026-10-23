from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from crewplane.core.state_paths import (
    EXECUTION_RESULTS_DIR_NAME,
    EXECUTION_STAGES_DIR_NAME,
)

from .git_policy import portable_path_key
from .naming import safe_file_component

type WorktreeCacheFamily = Literal["workspaces", "review-workspaces"]

WORKTREE_CACHE_FAMILY: Final = "workspaces"
REVIEW_WORKTREE_CACHE_FAMILY: Final = "review-workspaces"
SNAPSHOT_CACHE_FAMILY: Final = "snapshots"
LEGACY_WORKSPACE_CACHE_FAMILY: Final = "workspace-runs"


@dataclass(frozen=True)
class WorkspaceCacheLayout:
    family: str
    repository_scoped: bool = True
    node_scoped: bool = False

    def matches(
        self,
        parts: tuple[str, ...],
        owner: tuple[str, str],
        parent_slug: str | None = None,
    ) -> bool:
        prefix: tuple[str, ...] = (
            (self.family, *owner) if self.repository_scoped else (self.family, owner[1])
        )
        if self.node_scoped:
            if parent_slug is None:
                return False
            prefix += (safe_file_component(parent_slug),)
        return len(parts) == len(prefix) + 1 and parts[:-1] == prefix


WORKTREE_CACHE_LAYOUT = WorkspaceCacheLayout(WORKTREE_CACHE_FAMILY)
REVIEW_WORKTREE_CACHE_LAYOUT = WorkspaceCacheLayout(
    REVIEW_WORKTREE_CACHE_FAMILY, node_scoped=True
)
SNAPSHOT_CACHE_LAYOUT = WorkspaceCacheLayout(SNAPSHOT_CACHE_FAMILY)
WORKSPACE_CACHE_LAYOUTS = (
    WorkspaceCacheLayout(LEGACY_WORKSPACE_CACHE_FAMILY, repository_scoped=False),
    WORKTREE_CACHE_LAYOUT,
    SNAPSHOT_CACHE_LAYOUT,
    REVIEW_WORKTREE_CACHE_LAYOUT,
)


def worktree_cache_family(lineage_producer: bool) -> WorktreeCacheFamily:
    return WORKTREE_CACHE_FAMILY if lineage_producer else REVIEW_WORKTREE_CACHE_FAMILY


def workspace_cache_run_hierarchy(
    cache_root: Path,
    family: str,
    owner: tuple[str, str],
) -> tuple[Path, Path, Path, Path]:
    family_root = cache_root / family
    repository_root = family_root / owner[0]
    return cache_root, family_root, repository_root, repository_root / owner[1]


def workspace_invocation_path(
    run_root: Path,
    slug: str,
    parent_slug: str | None = None,
) -> Path:
    """Return the invocation path without creating directories."""
    if parent_slug is not None:
        run_root = run_root / safe_file_component(parent_slug)
    return run_root / slug


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
        state_dir / EXECUTION_STAGES_DIR_NAME,
        state_dir / EXECUTION_RESULTS_DIR_NAME,
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
