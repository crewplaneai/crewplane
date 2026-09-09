from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from rich.console import Console

from crewplane.core.config import Settings, load_config
from crewplane.core.state_paths import STATE_DIR_NAME, project_root_from_config_path
from crewplane.core.workspace.cache import (
    paths_overlap,
    workspace_cache_forbidden_roots,
    workspace_cache_root,
)
from crewplane.core.workspace.repository_identity import workspace_repository_id
from crewplane.runtime.workspace.cleanup import (
    WorkspaceCleanupFilter,
    parse_duration_seconds,
)

from ..paths import resolve_state_file
from ..run.workspace.git_source import (
    GitSourceContext,
    discover_git_context,
)
from ..run.workspace.source_types import WorkspacePolicyBuilder


@dataclass(frozen=True)
class WorkspaceCleanupOptions:
    config_file: Path | None
    successful: bool
    failed: bool
    cancelled: bool
    all_projects: bool
    run_key_name: str | None
    older_than: str | None
    orphans: bool


@dataclass(frozen=True)
class CleanupScope:
    repository_id: str | None
    git_context: GitSourceContext | None


@dataclass(frozen=True)
class WorkspaceCleanupContext:
    project_root: Path
    scope: CleanupScope
    cache_root: Path
    statuses: frozenset[str]
    run_key_name: str | None
    older_than_seconds: int | None
    all_projects: bool
    orphans: bool


def resolve_cleanup_workspace_context(
    console: Console,
    options: WorkspaceCleanupOptions,
) -> WorkspaceCleanupContext:
    config_file = resolve_state_file(
        options.config_file,
        "config.yml",
        "Run 'crewplane init' first.",
        console,
    )
    config = load_config(config_file)
    project_root = project_root_for_config(config_file)
    statuses = cleanup_statuses(
        options.successful,
        options.failed,
        options.cancelled,
    )
    validate_all_projects_filters(options.all_projects, statuses, options.orphans)
    scope = cleanup_scope(project_root, options.all_projects)
    return WorkspaceCleanupContext(
        project_root=project_root,
        scope=scope,
        cache_root=validate_cleanup_cache_root(
            config.settings,
            project_root,
            scope.git_context,
        ),
        statuses=statuses,
        run_key_name=options.run_key_name,
        older_than_seconds=parse_duration_seconds(options.older_than),
        all_projects=options.all_projects,
        orphans=options.orphans,
    )


def workspace_cleanup_filter(
    context: WorkspaceCleanupContext,
) -> WorkspaceCleanupFilter:
    return WorkspaceCleanupFilter(
        run_key_name=context.run_key_name,
        repository_id=context.scope.repository_id,
        expected_common_git_dir=(
            cast(GitSourceContext, context.scope.git_context).common_git_dir
            if not context.all_projects
            else None
        ),
        older_than_seconds=context.older_than_seconds,
        statuses=context.statuses,
        orphans=context.orphans,
    )


def project_root_for_config(config_file: Path) -> Path:
    return project_root_from_config_path(config_file)


def cleanup_repository_id(project_root: Path, all_projects: bool) -> str | None:
    return cleanup_scope(project_root, all_projects).repository_id


def cleanup_scope(project_root: Path, all_projects: bool) -> CleanupScope:
    builder = WorkspacePolicyBuilder()
    git_context = discover_git_context(project_root, builder)
    if all_projects:
        return CleanupScope(repository_id=None, git_context=git_context)
    if git_context is None:
        diagnostics = "; ".join(builder.errors)
        suffix = f" {diagnostics}" if diagnostics else ""
        raise RuntimeError(
            "Workspace cleanup is scoped to the current Git repository by default."
            f"{suffix} Use --all-projects to clean every repository bucket."
        )
    return CleanupScope(
        repository_id=workspace_repository_id(
            git_context.common_git_dir,
            project_root,
            git_context.object_format,
        ),
        git_context=git_context,
    )


def cleanup_statuses(
    successful: bool,
    failed: bool,
    cancelled: bool,
) -> frozenset[str]:
    statuses: set[str] = set()
    if successful:
        statuses.add("succeeded")
    if failed:
        statuses.add("failed")
    if cancelled:
        statuses.add("cancelled")
    return frozenset(statuses)


def validate_all_projects_filters(
    all_projects: bool,
    statuses: frozenset[str],
    orphans: bool,
) -> None:
    if not all_projects or (not statuses and not orphans):
        return
    raise RuntimeError(
        "--all-projects cannot be combined with --orphans or status filters "
        "(--successful, --failed, --cancelled) because those filters require "
        "current-project workspace-state artifacts."
    )


def validate_cleanup_cache_root(
    settings: Settings,
    project_root: Path,
    git_context: GitSourceContext | None,
) -> Path:
    cache_root = workspace_cache_root(settings.workspace.cache_root)
    if not cache_root.is_absolute():
        raise RuntimeError(
            "settings.workspace.cache_root must be absolute for workspace cleanup."
        )
    if cache_root.is_symlink():
        raise RuntimeError(
            f"Workspace cache root must not be a symlink: {cache_root.as_posix()}"
        )
    state_dir = project_root / STATE_DIR_NAME
    blocked_roots = workspace_cache_forbidden_roots(
        project_root,
        state_dir,
        git_context.active_git_dir if git_context is not None else None,
        git_context.common_git_dir if git_context is not None else None,
    )
    for blocked in blocked_roots:
        if paths_overlap(cache_root, blocked):
            raise RuntimeError(
                "Workspace cache root must not overlap the project, .crewplane, "
                f"or Git metadata paths: {cache_root.as_posix()}"
            )
    return cache_root
