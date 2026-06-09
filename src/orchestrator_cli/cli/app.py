from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from orchestrator_cli.core.config import Config, load_config
from orchestrator_cli.core.preflight import (
    PreflightCompilationPreview,
    load_workflow_source_for_preflight,
)
from orchestrator_cli.core.preflight.secrets import FingerprintKeyProvider
from orchestrator_cli.core.preflight.source import PreflightWorkflowSource
from orchestrator_cli.observability import ObservabilityHub
from orchestrator_cli.runtime.execution import execute_workflow

from . import workflow_runner
from .dry_run import preview_topological_waves, print_dry_run_plan
from .paths import (
    ORCHESTRATOR_DIR,
    WORKFLOWS_DIRNAME,
    ensure_orchestrator_dir,
    resolve_orchestrator_file,
    resolve_tasks_file,
)
from .templates import (
    CONFIG_TEMPLATE,
    DEFAULT_WORKFLOW_TEMPLATE,
    WORKFLOW_LIBRARY_TEMPLATE_DIR,
    create_template_file,
    discover_workflow_library_assets,
)

app = typer.Typer(name="orchestrator", help="Multi-agent CLI orchestrator")


@dataclass(frozen=True)
class CliWorkflowPaths:
    tasks: Path
    config: Path
    project_root: Path
    orchestrator_dir: Path


@dataclass(frozen=True)
class CliWorkflowContext:
    paths: CliWorkflowPaths
    config: Config
    source: PreflightWorkflowSource


def _create_template_file(
    file_path: Path, template_path: Path, console: Console
) -> None:
    create_template_file(file_path, template_path, console)


def _resolve_tasks_file(
    override_path: Path | None, init_hint: str, console: Console
) -> Path:
    return resolve_tasks_file(override_path, init_hint, console)


def _resolve_orchestrator_file(
    override_path: Path | None,
    filename: str,
    init_hint: str,
    console: Console,
) -> Path:
    return resolve_orchestrator_file(override_path, filename, init_hint, console)


def _resolve_cli_workflow_paths(
    tasks_file: Path | None,
    config_file: Path | None,
    console: Console,
) -> CliWorkflowPaths:
    tasks = _resolve_tasks_file(tasks_file, "Run 'orchestrator init' first.", console)
    config = _resolve_orchestrator_file(
        config_file,
        "config.yml",
        "Run 'orchestrator init' first.",
        console,
    )
    project_root = _project_root_from_config_path(config)
    return CliWorkflowPaths(
        tasks=tasks,
        config=config,
        project_root=project_root,
        orchestrator_dir=project_root / ORCHESTRATOR_DIR,
    )


def _project_root_from_config_path(config_path: Path) -> Path:
    config_parent = config_path.resolve(strict=False).parent
    if config_parent.name == ORCHESTRATOR_DIR:
        return config_parent.parent
    return config_parent


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
    orchestrator_dir: Path,
    force: bool,
    no_live: bool,
    console: Console,
) -> None:
    await workflow_runner.execute_workflow_run(
        config=config,
        source=source,
        project_root=project_root,
        orchestrator_dir=orchestrator_dir,
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
        orchestrator_dir=context.paths.orchestrator_dir,
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
        orchestrator_dir=context.paths.orchestrator_dir,
        check_cli_availability=True,
        which_fn=shutil.which,
    )
    workflow_runner.raise_for_preflight_preview_errors(preview, console)
    return preview


def _install_template_assets(
    source_root: Path,
    destination_root: Path,
    assets: list[Path],
    console: Console,
) -> None:
    for relative_path in assets:
        source_template = source_root / relative_path
        output_path = destination_root / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _create_template_file(output_path, source_template, console)


@app.command()
def init() -> None:
    """Initialize a new .orchestrator directory with default config files."""
    console = Console()
    orchestrator_dir = ensure_orchestrator_dir()
    workflows_dir = orchestrator_dir / WORKFLOWS_DIRNAME
    workflows_dir.mkdir(exist_ok=True)
    workflow_library_dir = workflows_dir / "example-templates"
    workflow_library_dir.mkdir(exist_ok=True)

    _create_template_file(orchestrator_dir / "config.yml", CONFIG_TEMPLATE, console)
    _create_template_file(
        workflows_dir / "code-review-example.task.md",
        DEFAULT_WORKFLOW_TEMPLATE,
        console,
    )
    _install_template_assets(
        WORKFLOW_LIBRARY_TEMPLATE_DIR,
        workflow_library_dir,
        discover_workflow_library_assets(),
        console,
    )
    key_result = FingerprintKeyProvider(orchestrator_dir).load_key("persist_if_needed")
    for diagnostic in key_result.diagnostics:
        console.print(f"[yellow]WARN[/] {diagnostic.message}")

    console.print("\n[bold]Initialized .orchestrator directory.[/]")
    console.print("Edit the config and task files, then run: [cyan]orchestrator run[/]")


@app.command()
def run(
    tasks_file: Annotated[
        Path | None,
        typer.Option(
            "--tasks",
            "-t",
            exists=True,
            readable=True,
            file_okay=True,
            dir_okay=False,
            help=(
                "Path to workflow file "
                "(default: a single .orchestrator/workflows/*.task.md)"
            ),
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            exists=True,
            readable=True,
            file_okay=True,
            dir_okay=False,
            help="Path to config file (default: .orchestrator/config.yml)",
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
            return
    except typer.Exit:
        raise
    except Exception as exc:
        if not dry_run:
            workflow_runner.write_early_preflight_failure_run(
                resolved_tasks_file,
                str(exc),
                project_root=paths.project_root if paths is not None else None,
                orchestrator_dir=(
                    paths.orchestrator_dir if paths is not None else None
                ),
            )
        console.print(f"[red]✗[/] Invalid: {exc}")
        raise typer.Exit(code=1) from exc

    try:
        asyncio.run(
            _execute_workflow(
                context.config,
                context.source,
                project_root=context.paths.project_root,
                orchestrator_dir=context.paths.orchestrator_dir,
                force=force,
                no_live=no_live,
                console=console,
            )
        )
    except workflow_runner.WorkflowCancelledByUser as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(code=130) from None


@app.command()
def validate(
    tasks_file: Annotated[
        Path | None,
        typer.Argument(
            exists=True,
            readable=True,
            file_okay=True,
            dir_okay=False,
            help=(
                "Path to workflow file to validate "
                "(default: a single .orchestrator/workflows/*.task.md)"
            ),
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            exists=True,
            readable=True,
            file_okay=True,
            dir_okay=False,
            help="Path to config file (default: .orchestrator/config.yml)",
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
