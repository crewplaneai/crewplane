from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from crewplane.core.config import Settings
from crewplane.core.workspace.cache import workspace_cache_root

from .git_source import GitSourceContext
from .source_types import WorkspacePolicyBuilder


def validate_cache_root(
    settings: Settings,
    project_root: Path,
    state_dir: Path,
    git_context: GitSourceContext,
    builder: WorkspacePolicyBuilder,
) -> None:
    cache_root = workspace_cache_root(settings.workspace.cache_root)
    if not cache_root.is_absolute():
        builder.errors.append(
            "settings.workspace.cache_root must be absolute when workspace "
            "isolation is enabled."
        )
        return
    if cache_root.exists() and cache_root.is_symlink():
        builder.errors.append(
            f"Workspace cache root must not be a symlink: {cache_root.as_posix()}"
        )
        return
    blocked_roots = (
        project_root,
        state_dir,
        state_dir / "execution-stages",
        state_dir / "execution-results",
        state_dir / "locks",
        git_context.active_git_dir,
        git_context.common_git_dir,
    )
    for blocked in blocked_roots:
        if paths_overlap(cache_root, blocked):
            builder.errors.append(
                "Workspace cache root must not overlap the project, .crewplane, "
                f"or Git metadata paths: {cache_root.as_posix()}"
            )
            return


def paths_overlap(left: Path, right: Path) -> bool:
    resolved_left = left.expanduser().resolve(strict=False)
    resolved_right = right.expanduser().resolve(strict=False)
    case_left = Path(normalized_casefold_path(resolved_left))
    case_right = Path(normalized_casefold_path(resolved_right))
    return (
        resolved_left == resolved_right
        or resolved_left.is_relative_to(resolved_right)
        or resolved_right.is_relative_to(resolved_left)
        or case_left == case_right
        or case_left.is_relative_to(case_right)
        or case_right.is_relative_to(case_left)
    )


def normalized_casefold_path(path: Path) -> str:
    return unicodedata.normalize("NFC", os.fspath(path)).casefold()
