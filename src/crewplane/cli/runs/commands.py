"""Read-only project-local saved execution inspection commands."""

from __future__ import annotations

from typing import Annotated, NoReturn

import typer
from rich.console import Console

from crewplane.artifacts.run_history import RunHistoryScan, scan_run_history
from crewplane.core.state_paths import get_state_dir

from .listing import print_saved_runs
from .logs import describe_selected_log, select_saved_log
from .reports import open_saved_target, show_saved_report
from .selection import resolve_run, warn_excluded_runs
from .streaming import stream_saved_log

runs_app = typer.Typer(
    help="Inspect saved executions under this project's .crewplane, without running or repairing them."
)
RunSelector = Annotated[
    str,
    typer.Argument(
        metavar="RUN",
        help="latest, an exact full key, an exact run ID, or a unique key/ID prefix.",
    ),
]


def _history() -> RunHistoryScan:
    history = scan_run_history(get_state_dir())
    warn_excluded_runs(history)
    return history


def _fail(error: Exception) -> NoReturn:
    typer.echo(f"Saved run inspection failed: {error}", err=True)
    raise typer.Exit(code=1) from error


@runs_app.command("list")
def list_runs() -> None:
    """List recorded outcomes, manifest elapsed, reuse, and saved-evidence attention.

    running (recorded) is not liveness. Elapsed is the finalized manifest interval,
    not the saved summary's execution-only time. No configuration is loaded.
    """
    try:
        print_saved_runs(_history(), Console(markup=False, highlight=False))
    except (OSError, ValueError, RuntimeError) as exc:
        _fail(exc)


@runs_app.command("show")
def show_run(run: RunSelector) -> None:
    """Display the saved Markdown report, which may predate manifest finalization."""
    try:
        show_saved_report(
            resolve_run(_history(), run), Console(markup=False, highlight=False)
        )
    except (OSError, ValueError, RuntimeError) as exc:
        _fail(exc)


@runs_app.command("open")
def open_run(
    run: RunSelector,
    results: Annotated[
        bool,
        typer.Option(
            "--results",
            help="Open the existing results directory instead of the saved summary.",
        ),
    ] = False,
) -> None:
    """Submit a nonblocking local open request. Use show on headless systems."""
    try:
        path = open_saved_target(resolve_run(_history(), run), results)
        typer.echo(f"Open request submitted: {path}")
    except (OSError, ValueError, RuntimeError) as exc:
        _fail(exc)


@runs_app.command("logs")
def logs_run(
    run: RunSelector,
    node: Annotated[
        str | None,
        typer.Option(
            "--node",
            help="Exact logical node ID; select its latest recorded invocation log.",
        ),
    ] = None,
    follow: Annotated[
        bool,
        typer.Option(
            "--follow",
            help="Follow only this file's appends until Ctrl-C, including at terminal EOF; no rotation.",
        ),
    ] = False,
) -> None:
    """Print raw saved events or one provider log. Selection details go to stderr."""
    try:
        history = _history()
        selection = select_saved_log(resolve_run(history, run), node, history)
        describe_selected_log(selection)
        stream_saved_log(selection.source.run_dir, selection.path, follow)
    except (OSError, ValueError, RuntimeError) as exc:
        _fail(exc)
