from pathlib import Path
from stat import S_ISREG
from typing import Never

import typer
from rich.console import Console
from rich.text import Text

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


def resolve_tasks_file(
    override_path: Path | None,
    init_hint: str,
    console: Console,
    project_root: Path | None = None,
) -> Path:
    try:
        base = project_root if project_root is not None else Path.cwd().resolve()
        if override_path is not None:
            return _existing_file(_absolute_input_path(override_path, base))
        workflows_dir = get_state_dir(base) / WORKFLOWS_DIRNAME
        try:
            workflow_files = sorted(
                path for path in workflows_dir.iterdir() if path.match("*.task.md")
            )
        except FileNotFoundError:
            workflow_files = []
        if len(workflow_files) == 1:
            return _existing_file(workflow_files[0])
    except (OSError, ValueError, RuntimeError) as exc:
        _path_failure(console, exc)

    if len(workflow_files) > 1:
        console.print(
            Text(
                "Error: Multiple workflow files found in "
                f"{workflows_dir}. Pass --tasks to select one explicitly."
            )
        )
        raise typer.Exit(code=1)

    console.print(
        Text(
            "Error: No workflow file found. Expected exactly one "
            f"'.task.md' file in {workflows_dir}, or pass --tasks. {init_hint}"
        )
    )
    raise typer.Exit(code=1)


def resolve_state_file(
    override_path: Path | None,
    filename: str,
    init_hint: str,
    console: Console,
) -> Path:
    try:
        invocation_dir = Path.cwd().resolve()
        if override_path is not None:
            return _existing_file(_absolute_input_path(override_path, invocation_dir))
        for directory in (invocation_dir, *invocation_dir.parents):
            candidate = directory / STATE_DIR_NAME / filename
            try:
                candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ValueError(f"Cannot inspect config {candidate}: {exc}") from exc
            return _existing_file(candidate)
    except (OSError, ValueError, RuntimeError) as exc:
        _path_failure(console, exc)
    console.print(
        Text(
            f"Error: No {STATE_DIR_NAME}/{filename} found from {invocation_dir} "
            "through its ancestors. Run 'crewplane init' in the intended project "
            f"root or pass --config. {init_hint}"
        )
    )
    raise typer.Exit(code=1)


def _existing_file(path: Path) -> Path:
    try:
        candidate = path.resolve(strict=True)
        if not S_ISREG(candidate.stat().st_mode):
            raise ValueError("not a regular file")
        with candidate.open("rb"):
            pass
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(f"Cannot read file {path}: {exc}") from exc
    return candidate


def _absolute_input_path(path: Path, base: Path) -> Path:
    candidate = path.expanduser()
    if candidate.is_absolute():
        return candidate
    return base / candidate


def _path_failure(console: Console, exc: Exception) -> Never:
    console.print("[red]Error:[/]", Text(str(exc)))
    raise typer.Exit(code=1) from exc
