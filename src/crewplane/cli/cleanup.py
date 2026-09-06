from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from crewplane.runtime.workspace.cleanup import WorkspaceCleanupResult
from crewplane.runtime.workspace.worktree.ref_cleanup import (
    WorkspaceRunRefCleanup,
    workspace_ref_cleanup_for_project,
)

from .workspace_cleanup import context as _cleanup_context
from .workspace_cleanup.context import (
    WorkspaceCleanupContext,
    WorkspaceCleanupOptions,
)
from .workspace_cleanup.execution import execute_workspace_cleanup

cleanup_app = typer.Typer(help="Remove generated crewplane runtime state.")


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
        context = resolve_cleanup_workspace_context(
            console=console,
            config_file=config_file,
            successful=successful,
            failed=failed,
            cancelled=cancelled,
            all_projects=all_projects,
            run_key_name=run_key_name,
            older_than=older_than,
            orphans=orphans,
        )
        destructive = yes and not dry_run
        warn_all_projects_cleanup(console, context.all_projects)
        result = execute_workspace_cleanup(context, destructive)
        write_cleanup_result(console, result, destructive)
    except Exception as exc:
        console.print(f"[red]Cleanup failed:[/] {exc}")
        raise typer.Exit(code=1) from exc


def resolve_cleanup_workspace_context(
    console: Console,
    config_file: Path | None,
    successful: bool,
    failed: bool,
    cancelled: bool,
    all_projects: bool,
    run_key_name: str | None,
    older_than: str | None,
    orphans: bool,
) -> WorkspaceCleanupContext:
    options = WorkspaceCleanupOptions(
        config_file=config_file,
        successful=successful,
        failed=failed,
        cancelled=cancelled,
        all_projects=all_projects,
        run_key_name=run_key_name,
        older_than=older_than,
        orphans=orphans,
    )
    return _cleanup_context.resolve_cleanup_workspace_context(console, options)


def write_cleanup_result(
    console: Console,
    result: WorkspaceCleanupResult,
    destructive: bool,
) -> None:
    verb = "Removed" if destructive else "Would remove"
    console.print(
        f"{verb} {result.selected_count} workspace path(s) under "
        f"{result.cache_root.as_posix()}."
    )
    for entry in result.entries:
        console.print(
            f"  - {entry.path.as_posix()} "
            f"({entry.size_bytes} bytes, run={entry.run_key_name}, "
            f"status={entry.status or 'orphan'})"
        )
        if entry.retained_reason is not None:
            console.print(f"    retained: {entry.retained_reason}")
    if destructive and result.removed_ref_count:
        console.print(f"Removed {result.removed_ref_count} run-owned Git ref(s).")


def warn_all_projects_cleanup(console: Console, all_projects: bool) -> None:
    if not all_projects:
        return
    console.print(
        "[yellow]Warning:[/] --all-projects cannot verify cross-project run "
        "ownership or activity."
    )
    console.print(
        "Passing --yes without --dry-run explicitly authorizes deletion "
        "across every repository bucket."
    )


def workspace_ref_cleanup(
    project_root: Path,
    all_projects: bool,
) -> WorkspaceRunRefCleanup | None:
    if all_projects:
        return None
    return workspace_ref_cleanup_for_project(project_root)
