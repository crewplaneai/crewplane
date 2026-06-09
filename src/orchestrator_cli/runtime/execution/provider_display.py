from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from orchestrator_cli.architecture.contracts import (
    AgentInvoker,
    ConsoleMessageSink,
    InvocationContext,
)
from orchestrator_cli.core.config import AgentConfig

from .execution_activity import ExecutionTelemetry
from .execution_console import execution_console, progress_context, should_print_console


@dataclass(frozen=True)
class ProviderCallDisplay:
    telemetry: ExecutionTelemetry | None
    progress_description: str | None = None
    show_console_summary: bool = True


def print_provider_start(
    display: ProviderCallDisplay,
    role_label: str,
    task_id: str,
    provider_name: str,
    model: str | None,
) -> None:
    if not display.show_console_summary or not should_print_console(display.telemetry):
        return
    model_label = model if model is not None else "provider default"
    console = execution_console(display.telemetry)
    console.print(
        f"Running {role_label}: [bold]{task_id}[/] with model [cyan]{model_label}[/]"
    )
    console.print(f"[dim]→ starting {provider_name}[/]")


def print_provider_finish(
    display: ProviderCallDisplay,
    task_id: str,
    output_file: Path,
) -> None:
    if not display.show_console_summary or not should_print_console(display.telemetry):
        return
    execution_console(display.telemetry).print(
        f"[green]✓[/] {task_id} → {output_file.name}"
    )


def provider_console_message_sink(
    display: ProviderCallDisplay,
) -> ConsoleMessageSink | None:
    if not should_print_console(display.telemetry):
        return None
    return execution_console(display.telemetry).print


async def invoke_with_display(
    display: ProviderCallDisplay,
    invoker: AgentInvoker,
    agent_config: AgentConfig,
    model: str | None,
    prompt: str,
    output_file: Path,
    log_file: Path | None,
    invocation_context: InvocationContext,
) -> None:
    invoke = _build_invoke_callback(
        invoker,
        agent_config,
        model,
        prompt,
        output_file,
        log_file,
        invocation_context,
    )
    if display.progress_description is None:
        await invoke()
        return

    with progress_context(
        display.progress_description,
        selected_console=execution_console(display.telemetry),
        enabled=should_print_console(display.telemetry),
    ):
        await invoke()


def _build_invoke_callback(
    invoker: AgentInvoker,
    agent_config: AgentConfig,
    model: str | None,
    prompt: str,
    output_file: Path,
    log_file: Path | None,
    invocation_context: InvocationContext,
) -> Callable[[], Awaitable[None]]:
    async def invoke() -> None:
        await invoker.invoke(
            agent_config,
            model,
            prompt,
            output_file,
            log_file,
            invocation_context=invocation_context,
        )

    return invoke
