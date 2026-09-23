from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Never

import typer
from rich.console import Console

from crewplane.core.config import Config, load_config
from crewplane.core.preflight import (
    PreflightCompilationPreview,
    load_workflow_source_for_preflight,
)
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.state_paths import STATE_DIR_NAME, project_root_from_config_path
from crewplane.core.workflow.validation.workspace import (
    validate_repeat_force_run_workspace,
)

from . import workflow_runner
from .paths import resolve_state_file, resolve_tasks_file


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


def resolve_cli_workflow_paths(
    tasks_file: Path | None,
    config_file: Path | None,
    console: Console,
) -> CliWorkflowPaths:
    config = resolve_state_file(
        config_file,
        "config.yml",
        "Run 'crewplane init' first.",
        console,
    )
    project_root = project_root_from_config_path(config)
    tasks = resolve_tasks_file(
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


def load_cli_workflow_context(
    paths: CliWorkflowPaths,
    require_project_root: bool = False,
) -> CliWorkflowContext:
    config = load_config(paths.config)
    source = load_workflow_source_for_preflight(
        paths.tasks,
        project_root=paths.project_root,
    )
    if require_project_root or source.workflow.repeat_force_run_count is not None:
        validate_repeat_force_run_workspace(source.workflow, config)
    return CliWorkflowContext(
        paths=paths,
        config=config,
        source=source,
    )


def compile_preview_for_context(
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


def compile_validate_preview_for_context(
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


def raise_context_loading_failure(
    exc: Exception,
    console: Console,
    tasks_file: Path | None = None,
    paths: CliWorkflowPaths | None = None,
    write_diagnostics: bool = False,
) -> Never:
    if write_diagnostics:
        workflow_runner.write_early_preflight_failure_run(
            paths.tasks if paths is not None else tasks_file,
            str(exc),
            project_root=paths.project_root if paths is not None else None,
            state_dir=paths.state_dir if paths is not None else None,
        )
    console.print(f"[red]✗[/] Invalid: {exc}")
    raise typer.Exit(code=1) from exc
