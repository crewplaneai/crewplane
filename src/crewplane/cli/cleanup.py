from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from crewplane.core.config import Settings, load_config
from crewplane.core.state_paths import STATE_DIR_NAME, project_root_from_config_path
from crewplane.core.workspace.cache import workspace_cache_root
from crewplane.runtime.workspace.cleanup import (
    WorkspaceCleanupFilter,
    WorkspaceStatusLookup,
    cleanup_workspace_cache,
    parse_duration_seconds,
)
from crewplane.runtime.workspace.worktree.ref_cleanup import (
    workspace_ref_cleanup_for_project,
)

from .paths import resolve_state_file
from .run.workspace.cache_policy import paths_overlap
from .run.workspace.git_source import (
    GitSourceContext,
    discover_git_context,
    repository_id,
)
from .run.workspace.source_types import WorkspacePolicyBuilder

cleanup_app = typer.Typer(help="Remove generated crewplane runtime state.")


@dataclass(frozen=True)
class CleanupScope:
    repository_id: str | None
    git_context: GitSourceContext | None


@cleanup_app.command("workspaces")
def cleanup_workspaces(
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            file_okay=True,
            dir_okay=False,
            help="Path to config file (default: .crewplane/config.yml)",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show workspaces that would be removed."),
    ] = False,
    run_key_name: Annotated[
        str | None,
        typer.Option("--run", help="Only clean workspaces for this run key."),
    ] = None,
    older_than: Annotated[
        str | None,
        typer.Option(
            "--older-than",
            help="Only clean entries older than a duration like 30m, 12h, or 7d.",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm destructive workspace cleanup."),
    ] = False,
    successful: Annotated[
        bool,
        typer.Option("--successful", help="Only clean succeeded workspace states."),
    ] = False,
    failed: Annotated[
        bool,
        typer.Option("--failed", help="Only clean failed workspace states."),
    ] = False,
    cancelled: Annotated[
        bool,
        typer.Option("--cancelled", help="Only clean cancelled workspace states."),
    ] = False,
    orphans: Annotated[
        bool,
        typer.Option(
            "--orphans", help="Only clean cache paths without workspace state."
        ),
    ] = False,
    all_projects: Annotated[
        bool,
        typer.Option(
            "--all-projects",
            help="Clean every repository bucket under the workspace cache.",
        ),
    ] = False,
) -> None:
    console = Console()
    try:
        resolved_config_file = resolve_state_file(
            config_file,
            "config.yml",
            "Run 'crewplane init' first.",
            console,
        )
        config = load_config(resolved_config_file)
        project_root = project_root_for_config(resolved_config_file)
        settings = config.settings if config.settings is not None else Settings()
        statuses = cleanup_statuses(successful, failed, cancelled)
        validate_all_projects_filters(all_projects, statuses, orphans)
        scope = cleanup_scope(project_root, all_projects)
        cache_root = validate_cleanup_cache_root(
            settings,
            project_root,
            scope.git_context,
        )
        older_than_seconds = parse_duration_seconds(older_than)
    except Exception as exc:
        console.print(f"[red]Cleanup failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    destructive = yes and not dry_run

    result = cleanup_workspace_cache(
        cache_root,
        WorkspaceCleanupFilter(
            run_key_name=run_key_name,
            repository_id=scope.repository_id,
            expected_common_git_dir=(
                scope.git_context.common_git_dir if not all_projects else None
            ),
            older_than_seconds=older_than_seconds,
            statuses=statuses,
            orphans=orphans,
        ),
        dry_run=not destructive,
        status_lookup=cleanup_status_lookup(project_root, all_projects),
        ref_cleanup=(
            None if all_projects else workspace_ref_cleanup_for_project(project_root)
        ),
    )
    verb = "Removed" if destructive else "Would remove"
    console.print(
        f"{verb} {len(result.entries)} workspace path(s) under "
        f"{result.cache_root.as_posix()}."
    )
    for entry in result.entries:
        console.print(
            f"  - {entry.path.as_posix()} "
            f"({entry.size_bytes} bytes, run={entry.run_key_name}, "
            f"status={entry.status or 'orphan'})"
        )
    if destructive and result.removed_ref_count:
        console.print(f"Removed {result.removed_ref_count} run-owned Git ref(s).")


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
        repository_id=repository_id(git_context, project_root),
        git_context=git_context,
    )


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
    if cache_root.exists() and cache_root.is_symlink():
        raise RuntimeError(
            f"Workspace cache root must not be a symlink: {cache_root.as_posix()}"
        )
    state_dir = project_root / STATE_DIR_NAME
    blocked_roots = [
        project_root,
        state_dir,
        state_dir / "execution-stages",
        state_dir / "execution-results",
        state_dir / "locks",
    ]
    if git_context is not None:
        blocked_roots.extend([git_context.active_git_dir, git_context.common_git_dir])
    for blocked in blocked_roots:
        if paths_overlap(cache_root, blocked):
            raise RuntimeError(
                "Workspace cache root must not overlap the project, .crewplane, "
                f"or Git metadata paths: {cache_root.as_posix()}"
            )
    return cache_root


def cleanup_status_lookup(
    project_root: Path,
    all_projects: bool,
) -> WorkspaceStatusLookup:
    if all_projects:
        return unknown_workspace_status_lookup
    return workspace_status_lookup(project_root)


def unknown_workspace_status_lookup(
    run_key_name: str,  # noqa: ARG001 - Required by WorkspaceStatusLookup.
    cache_key: str,  # noqa: ARG001 - Required by WorkspaceStatusLookup.
) -> str:
    return "unknown"


def workspace_status_lookup(project_root: Path) -> WorkspaceStatusLookup:
    stage_root = project_root / ".crewplane" / "execution-stages"
    cache: dict[tuple[str, str], str] | None = None

    def lookup(run_key_name: str, cache_key: str) -> str | None:
        nonlocal cache
        if cache is None:
            cache = load_workspace_statuses(stage_root)
        return cache.get((run_key_name, cache_key))

    return lookup


def load_workspace_statuses(stage_root: Path) -> dict[tuple[str, str], str]:
    statuses: dict[tuple[str, str], str] = {}
    if not stage_root.is_dir():
        return statuses
    for state_path in stage_root.glob("*/**/workspace-state*.json"):
        if not state_path.is_file() or state_path.is_symlink():
            continue
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        run_key_name = payload.get("run_key_name")
        status = payload.get("status")
        workspace = payload.get("workspace")
        if not (
            isinstance(run_key_name, str)
            and isinstance(status, str)
            and isinstance(workspace, dict)
        ):
            continue
        cache_key = workspace.get("cache_key")
        if isinstance(cache_key, str):
            statuses[(run_key_name, cache_key)] = status
    return statuses
