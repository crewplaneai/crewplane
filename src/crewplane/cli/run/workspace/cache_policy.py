from __future__ import annotations

from pathlib import Path

from crewplane.core.config import Settings
from crewplane.core.workspace.cache import (
    paths_overlap,
    workspace_cache_forbidden_roots,
    workspace_cache_root,
)

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
    if cache_root.is_symlink():
        builder.errors.append(
            f"Workspace cache root must not be a symlink: {cache_root.as_posix()}"
        )
        return
    blocked_roots = workspace_cache_forbidden_roots(
        project_root,
        state_dir,
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
