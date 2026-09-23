from __future__ import annotations

import asyncio
import io
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.text import Text

from crewplane.artifacts.locks import ResumeLockError
from crewplane.core.config import Config
from crewplane.core.platform import is_native_windows
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.observability import ObservabilityHub
from crewplane.observability.observer import Observer
from crewplane.observability.types import WorkflowTopology
from crewplane.runtime.execution import WorkflowExecutionError, execute_workflow

from . import workflow_runner
from .cleanup import cleanup_app
from .dry_run import preview_topological_waves, print_dry_run_plan
from .onboarding import ONBOARDING_COMMAND_HELP, run_onboarding_command
from .project_init import initialize_project_templates
from .run.observability import ObservabilityHubInstance
from .run.resume import print_dry_run_resume_advisory
from .update import UpdateError, installed_package_identity, update_crewplane
from .workflow_context import (
    CliWorkflowContext,
    CliWorkflowPaths,
    compile_preview_for_context,
    compile_validate_preview_for_context,
    load_cli_workflow_context,
    raise_context_loading_failure,
    resolve_cli_workflow_paths,
)

app = typer.Typer(name="crewplane", help="Multi-agent workflow runner")
app.add_typer(cleanup_app, name="cleanup")


def _configure_output_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")


def _print_version(show_version: bool) -> None:
    if not show_version:
        return

    try:
        package_name, package_version = installed_package_identity()
    except UpdateError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{package_name} {package_version}")
    raise typer.Exit()


def _update_package(update_package: bool) -> None:
    if not update_package:
        return

    _configure_output_encoding()
    console = Console()
    try:
        exit_code = update_crewplane(console)
    except UpdateError as exc:
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from exc

    raise typer.Exit(exit_code)


@app.callback()
def main(
    version: Annotated[  # noqa: ARG001 - handled by the eager option callback
        bool,
        typer.Option(
            "--version",
            "-v",
            callback=_print_version,
            help="Show the installed Crewplane package version and exit.",
            is_eager=True,
        ),
    ] = False,
    update: Annotated[  # noqa: ARG001 - handled by the eager option callback
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
    _configure_output_encoding()


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
        observability_hub_cls=_create_observability_hub,
        which_fn=shutil.which,
    )


async def _execute_workflow_passes(
    context: CliWorkflowContext,
    force: bool,
    no_live: bool,
    console: Console,
) -> None:
    paths = context.paths
    repeat_count = context.source.workflow.repeat_force_run_count
    effective_force = force or repeat_count is not None
    for pass_index in range(repeat_count or 1):
        if repeat_count is not None:
            console.print(f"Run {pass_index + 1} of {repeat_count} (fresh)")
        if pass_index:
            try:
                context = load_cli_workflow_context(paths, require_project_root=True)
            except Exception as exc:
                raise_context_loading_failure(
                    exc, console, paths=paths, write_diagnostics=True
                )
        await _execute_workflow(
            context.config,
            context.source,
            project_root=paths.project_root,
            state_dir=paths.state_dir,
            force=effective_force,
            no_live=no_live,
            console=console,
        )


def _create_observability_hub(
    workflow_topology: WorkflowTopology,
    run_id: str,
    observers: list[Observer],
    refresh_per_second: int = 4,
    warning_sink: Callable[[str], None] | None = None,
) -> ObservabilityHubInstance:
    return ObservabilityHub(
        workflow_topology=workflow_topology,
        run_id=run_id,
        observers=observers,
        refresh_per_second=refresh_per_second,
        warning_sink=warning_sink,
    )


@app.command()
def init() -> None:
    """Initialize a new .crewplane directory with default config files."""
    console = Console()
    if is_native_windows():
        console.print(
            "[yellow]Warning:[/] Native Windows is not officially supported. "
            "Crewplane may work, but unexpected issues can occur. "
            "Use WSL for a supported environment."
        )
    initialize_project_templates(console)

    console.print("\n[bold]Initialized .crewplane directory.[/]")
    console.print(
        "First run uses deterministic mock execution; no provider CLIs are required."
    )
    console.print("\n[bold]After the provider-free first run:[/]")
    console.print(
        "  The onboarding command prepares your selected providers "
        "when generated defaults are unchanged."
    )
    console.print("  It will not start provider CLIs or authenticate providers.")
    console.print("\n[bold]Manual setup for customized projects:[/]")
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
    """Prepare real providers after the provider-free first run."""
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
    paths: CliWorkflowPaths | None = None
    try:
        paths = resolve_cli_workflow_paths(tasks_file, config_file, console)
        context = load_cli_workflow_context(paths)

        if dry_run:
            preview = compile_preview_for_context(
                context, no_live=True, console=console
            )
            repeat_count = context.source.workflow.repeat_force_run_count
            if repeat_count is not None:
                console.print(f"Planned fresh passes: {repeat_count}")
                console.print(
                    "First pass preview; later edits can change inputs and validation."
                )
            print_dry_run_plan(preview, console)
            print_dry_run_resume_advisory(
                config=context.config,
                source=context.source,
                preview=preview,
                project_root=context.paths.project_root,
                state_dir=context.paths.state_dir,
                force=force or repeat_count is not None,
                console=console,
            )
            return
    except typer.Exit:
        raise
    except Exception as exc:
        raise_context_loading_failure(
            exc, console, tasks_file, paths, write_diagnostics=not dry_run
        )

    try:
        asyncio.run(
            _execute_workflow_passes(
                context,
                force=force,
                no_live=no_live,
                console=console,
            )
        )
    except workflow_runner.WorkflowCancelledByUser as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(code=130) from None
    except WorkflowExecutionError as exc:
        console.print(Text.assemble(("✗", "red"), f" {exc}"))
        raise typer.Exit(code=1) from None
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
    paths = resolve_cli_workflow_paths(tasks_file, config_file, console)
    console.print(f"Validating {paths.tasks}...")
    try:
        context = load_cli_workflow_context(paths)
        preview = compile_validate_preview_for_context(context, console)
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
        raise_context_loading_failure(exc, console)

    console.print(
        f"[green]✓[/] Valid: {len(preview.nodes)} nodes across {len(waves)} execution wave(s)"
    )


if __name__ == "__main__":
    app()
