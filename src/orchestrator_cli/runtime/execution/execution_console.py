from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .execution_activity import ExecutionTelemetry


def should_print_console(telemetry: ExecutionTelemetry | None) -> bool:
    return telemetry is None or not telemetry.suppress_console_output


def execution_console(telemetry: ExecutionTelemetry | None) -> Console:
    if telemetry is None:
        return Console()
    return telemetry.console


@contextmanager
def progress_context(
    description: str,
    selected_console: Console,
    enabled: bool = True,
) -> Iterator[None]:
    if not enabled:
        yield
        return
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=selected_console,
        transient=True,
    ) as progress:
        progress.add_task(description=description, total=None)
        yield
