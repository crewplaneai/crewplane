from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from crewplane.artifacts.locks import ResumeLockError
from crewplane.core.config import Config, load_config
from crewplane.core.preflight import (
    PreflightCompilationPreview,
    load_workflow_source_for_preflight,
)
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.state_paths import STATE_DIR_NAME, project_root_from_config_path
from crewplane.observability import ObservabilityHub
from crewplane.runtime.execution import execute_workflow

from . import workflow_runner
from .cleanup import cleanup_app
from .dry_run import preview_topological_waves, print_dry_run_plan
from .onboarding import ONBOARDING_COMMAND_HELP, run_onboarding_command
from .paths import (
    resolve_state_file,
    resolve_tasks_file,
)
from .project_init import initialize_project_templates
from .run.resume import print_dry_run_resume_advisory
from .update import UpdateError, installed_package_identity, update_crewplane

app = typer.Typer(name="crewplane", help="Multi-agent workflow runner")
app.add_typer(cleanup_app, name="cleanup")


def _print_version(show_version: bool) -> None:
    if not show_version:
        return

    package_name, package_version = installed_package_identity()
    typer.echo(f"{package_name} {package_version}")
    raise typer.Exit()


def _update_package(update_package: bool) -> None:
    if not update_package:
        return

    console = Console()
    try:
        exit_code = update_crewplane(console)
    except UpdateError as exc:
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from exc

    raise typer.Exit(exit_code)


@app.callback()
def main(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-v",
            callback=_print_version,
            help="Show the installed Crewplane package version and exit.",
            is_eager=True,
        ),
    ] = False,
    _update: Annotated[
        bool,
        typer.Option(
            "--update",
            "-u",
            callback=_update_package,
            help="Update Crewplane through its owning package manager and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Multi-agent workflow runner."""


@dataclass(frozen=True)
class CliWorkflowPaths:
    tasks: Path
    config: Path
    project_root: Path
    state_dir: Path


@dataclass(frozen=True)
class CliWorkflowContext:
    paths: CliWorkflowPaths
    config: Config
    source: PreflightWorkflowSource


def _resolve_tasks_file(
    override_path: Path | None,
    init_hint: str,
    console: Console,
    project_root: Path | None = None,
) -> Path:
    return resolve_tasks_file(override_path, init_hint, console, project_root)


def _resolve_state_file(
    override_path: Path | None,
    filename: str,
    init_hint: str,
    console: Console,
) -> Path:
    return resolve_state_file(override_path, filename, init_hint, console)


def _resolve_cli_workflow_paths(
    tasks_file: Path | None,
    config_file: Path | None,
    console: Console,
) -> CliWorkflowPaths:
    config = _resolve_state_file(
        config_file,
        "config.yml",
        "Run 'crewplane init' first.",
        console,
    )
    project_root = project_root_from_config_path(config)
    tasks = _resolve_tasks_file(
        tasks_file,
        "Run 'crewplane init' first.",
        console,
        project_root,
    )
    return CliWorkflowPaths(
        tasks=tasks,
        config=config,
        project_root=project_root,
        state_dir=project_root / STATE_DIR_NAME,
    )


def _load_cli_workflow_context(paths: CliWorkflowPaths) -> CliWorkflowContext:
    config = load_config(paths.config)
    source = load_workflow_source_for_preflight(
        paths.tasks,
        project_root=paths.project_root,
    )
    return CliWorkflowContext(
        paths=paths,
        config=config,
        source=source,
    )


async def _execute_workflow(
    config: Config,
    source: PreflightWorkflowSource,
    project_root: Path,
    state_dir: Path,
    force: bool,
    no_live: bool,
    console: Console,
) -> None:
    await workflow_runner.execute_workflow_run(
        config=config,
        source=source,
        project_root=project_root,
        state_dir=state_dir,
        force=force,
        no_live=no_live,
        console=console,
        execute_workflow_impl=execute_workflow,
        observability_hub_cls=ObservabilityHub,
        which_fn=shutil.which,
    )


def _compile_preview_for_context(
    context: CliWorkflowContext,
    no_live: bool,
    console: Console,
) -> PreflightCompilationPreview:
    preview = workflow_runner.compile_workflow_preview(
        config=context.config,
        source=context.source,
        console=console,
        no_live=no_live,
        fingerprint_key_policy="read_only",
        project_root=context.paths.project_root,
        state_dir=context.paths.state_dir,
    )
    workflow_runner.raise_for_preflight_preview_errors(preview, console)
    return preview


def _compile_validate_preview_for_context(
    context: CliWorkflowContext,
    console: Console,
) -> PreflightCompilationPreview:
    preview = workflow_runner.compile_workflow_preview(
        config=context.config,
        source=context.source,
        console=console,
        no_live=True,
        fingerprint_key_policy="read_only",
        project_root=context.paths.project_root,
        state_dir=context.paths.state_dir,
        check_cli_availability=True,
        which_fn=shutil.which,
        workspace_real_execution=False,
    )
    workflow_runner.raise_for_preflight_preview_errors(preview, console)
    return preview


@app.command()
def init() -> None:
    """Initialize a new .crewplane directory with default config files."""
    console = Console()
    initialize_project_templates(console)

    console.print("\n[bold]Initialized .crewplane directory.[/]")
    console.print(
        "First run uses deterministic mock execution; no provider CLIs are required."
    )
    console.print("\n[bold]After the provider-free first run:[/]")
    console.print(
        "  The onboarding command prepares one real provider handoff "
        "when generated defaults are unchanged."
    )
    console.print("  It will not start provider CLIs or authenticate providers.")
    console.print("\n[bold]Manual setup for customized or multi-provider projects:[/]")
    console.print(
        "  Enable matching agents in [cyan].crewplane/config.yml[/], set "
        '[cyan]settings.integrations.invoker.implementation: "cli"[/], and set '
        "[cyan]settings.integrations.invoker.options: {}[/]."
    )
    console.print(
        "  Use [cyan].crewplane/workflows/example-templates/[/] as starting points "
        "for real-CLI workflows."
    )
    console.print("  Run an example with the compact dashboard:")
    console.print(
        "  [cyan]crewplane run --tasks "
        ".crewplane/workflows/example-templates/code-review-example.task.md[/]"
    )
    console.print(
        "  Docs: [cyan]https://github.com/crewplaneai/crewplane/blob/master/"
        "docs/index.md[/]"
    )
    console.print("\n[bold]Next:[/]")
    console.print("  [cyan]crewplane validate[/]")
    console.print("  [cyan]crewplane run[/]")
    console.print("  [cyan]crewplane onboarding[/]")


@app.command("onboarding", help=ONBOARDING_COMMAND_HELP)
def onboarding() -> None:
    """Prepare one real provider after the provider-free first run."""
    run_onboarding_command()


@app.command()
def run(
    tasks_file: Annotated[
        Path | None,
        typer.Option(
            "--tasks",
            "-t",
            file_okay=True,
            dir_okay=False,
            help=(
                "Path to workflow file "
                "(default: a single .crewplane/workflows/*.task.md)"
            ),
        ),
    ] = None,
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
        typer.Option(
            "--dry-run",
            "-n",
            help="Show execution plan without running",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Run even if an identical context manifest already exists.",
        ),
    ] = False,
    no_live: Annotated[
        bool,
        typer.Option(
            "--no-live",
            help="Disable live topology dashboard output.",
        ),
    ] = False,
) -> None:
    """Execute the workflow DAG."""
    console = Console()
    resolved_tasks_file: Path | None = tasks_file
    paths: CliWorkflowPaths | None = None
    try:
        paths = _resolve_cli_workflow_paths(tasks_file, config_file, console)
        resolved_tasks_file = paths.tasks
        context = _load_cli_workflow_context(paths)

        if dry_run:
            preview = _compile_preview_for_context(
                context, no_live=True, console=console
            )
            print_dry_run_plan(preview, console)
            print_dry_run_resume_advisory(
                config=context.config,
                source=context.source,
                preview=preview,
                project_root=context.paths.project_root,
                state_dir=context.paths.state_dir,
                force=force,
                console=console,
            )
            return
    except typer.Exit:
        raise
    except Exception as exc:
        if not dry_run:
            workflow_runner.write_early_preflight_failure_run(
                resolved_tasks_file,
                str(exc),
                project_root=paths.project_root if paths is not None else None,
                state_dir=(paths.state_dir if paths is not None else None),
            )
        console.print(f"[red]✗[/] Invalid: {exc}")
        raise typer.Exit(code=1) from exc

    try:
        asyncio.run(
            _execute_workflow(
                context.config,
                context.source,
                project_root=context.paths.project_root,
                state_dir=context.paths.state_dir,
                force=force,
                no_live=no_live,
                console=console,
            )
        )
    except workflow_runner.WorkflowCancelledByUser as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(code=130) from None
    except ResumeLockError as exc:
        console.print(f"[red]✗[/] Run lock unavailable: {exc}")
        console.print(
            "[yellow]Stop any matching crewplane run before retrying. "
            "If no run is active, remove .crewplane/locks and retry.[/]"
        )
        raise typer.Exit(code=1) from None


@app.command()
def validate(
    tasks_file: Annotated[
        Path | None,
        typer.Argument(
            file_okay=True,
            dir_okay=False,
            help=(
                "Path to workflow file to validate "
                "(default: a single .crewplane/workflows/*.task.md)"
            ),
        ),
    ] = None,
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
) -> None:
    """Validate a workflow definition file."""
    console = Console()
    paths = _resolve_cli_workflow_paths(tasks_file, config_file, console)
    console.print(f"Validating {paths.tasks}...")
    try:
        context = _load_cli_workflow_context(paths)
        preview = _compile_validate_preview_for_context(context, console)
        waves = preview_topological_waves(preview)
        if paths.tasks.suffix.lower() == ".md":
            console.print("[green]✓[/] Frontmatter: valid YAML")
            console.print("[green]✓[/] Schema: WorkflowFrontmatter")
            console.print(f"[green]✓[/] Nodes: {len(preview.nodes)} compiled")
            console.print(
                f"[green]✓[/] Dependencies: {len(preview.dependency_graph)} edges"
            )
        imported_workflow_count = max(0, len(context.source.referenced_workflows) - 1)
        if imported_workflow_count:
            console.print(
                "[green]✓[/] Imports: "
                f"{imported_workflow_count} imported workflow file(s) resolved"
            )
        console.print("[green]✓[/] Providers: references resolved")
        console.print("[green]✓[/] Preflight: compiled execution plan preview")
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(f"[red]✗[/] Invalid: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]✓[/] Valid: {len(preview.nodes)} nodes across {len(waves)} execution wave(s)"
    )


if __name__ == "__main__":
    app()
