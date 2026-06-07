from pathlib import Path

import typer
from rich.console import Console

ORCHESTRATOR_DIR = ".orchestrator"
WORKFLOWS_DIRNAME = "workflows"

__all__ = [
    "ORCHESTRATOR_DIR",
    "WORKFLOWS_DIRNAME",
    "ensure_orchestrator_dir",
    "get_orchestrator_dir",
    "resolve_orchestrator_file",
    "resolve_tasks_file",
]


def get_orchestrator_dir() -> Path:
    return Path.cwd() / ORCHESTRATOR_DIR


def ensure_orchestrator_dir() -> Path:
    orchestrator_dir = get_orchestrator_dir()
    orchestrator_dir.mkdir(exist_ok=True)
    return orchestrator_dir


def _default_orchestrator_file(name: str) -> Path:
    return get_orchestrator_dir() / name


def resolve_tasks_file(
    override_path: Path | None,
    init_hint: str,
    console: Console,
) -> Path:
    if override_path is not None:
        return override_path

    workflows_dir = get_orchestrator_dir() / WORKFLOWS_DIRNAME
    workflow_files = sorted(workflows_dir.glob("*.task.md"))
    if len(workflow_files) == 1:
        return workflow_files[0]
    if len(workflow_files) > 1:
        console.print(
            "[red]Error:[/] Multiple workflow files found in "
            f"{workflows_dir}. Pass --tasks to select one explicitly."
        )
        raise typer.Exit(code=1)

    console.print(
        "[red]Error:[/] No workflow file found. Expected exactly one "
        f"'.task.md' file in {workflows_dir}, or pass --tasks. {init_hint}"
    )
    raise typer.Exit(code=1)


def resolve_orchestrator_file(
    override_path: Path | None,
    filename: str,
    init_hint: str,
    console: Console,
) -> Path:
    if override_path is not None:
        return override_path

    default_path = _default_orchestrator_file(filename)
    if default_path.exists():
        return default_path

    console.print(f"[red]Error:[/] {default_path} not found. {init_hint}")
    raise typer.Exit(code=1)
