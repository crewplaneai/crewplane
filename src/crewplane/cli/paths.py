from pathlib import Path

import typer
from rich.console import Console

from crewplane.core.state_paths import (
    STATE_DIR_NAME,
    ensure_state_dir,
    get_state_dir,
)

WORKFLOWS_DIRNAME = "workflows"

__all__ = [
    "STATE_DIR_NAME",
    "WORKFLOWS_DIRNAME",
    "ensure_state_dir",
    "get_state_dir",
    "resolve_state_file",
    "resolve_tasks_file",
]


def _default_state_file(name: str) -> Path:
    return get_state_dir() / name


def resolve_tasks_file(
    override_path: Path | None,
    init_hint: str,
    console: Console,
    project_root: Path | None = None,
) -> Path:
    if override_path is not None:
        return _existing_file(
            override_path,
            "Workflow path",
            console,
        )

    workflows_dir = get_state_dir(project_root) / WORKFLOWS_DIRNAME
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


def resolve_state_file(
    override_path: Path | None,
    filename: str,
    init_hint: str,
    console: Console,
) -> Path:
    if override_path is not None:
        return _existing_file(
            override_path,
            "Config path",
            console,
        )

    default_path = _default_state_file(filename)
    if default_path.exists():
        return default_path

    console.print(f"[red]Error:[/] {default_path} not found. {init_hint}")
    raise typer.Exit(code=1)


def _existing_file(
    path: Path,
    path_description: str,
    console: Console,
) -> Path:
    try:
        candidate = _absolute_input_path(path)
        candidate = candidate.resolve(strict=False)
        if not candidate.exists():
            raise ValueError(f"{path_description} does not exist: {path}")
        if not candidate.is_file():
            raise ValueError(f"{path_description} is not a file: {path}")
        with candidate.open("rb"):
            pass
    except (OSError, ValueError) as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1) from exc
    return candidate


def _absolute_input_path(path: Path) -> Path:
    candidate = path.expanduser()
    if candidate.is_absolute():
        return candidate
    return Path.cwd().resolve(strict=False) / candidate
