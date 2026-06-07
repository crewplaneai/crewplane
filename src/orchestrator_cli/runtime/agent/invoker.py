from __future__ import annotations

from pathlib import Path

from orchestrator_cli.core.config import AgentConfig

from .invocation import command as invocation_command_module
from .invocation import loop as invocation_loop_module
from .types import CommandRunner, InvocationContext


async def invoke_agent(
    config: AgentConfig,
    model: str | None,
    prompt: str,
    output_file: Path,
    log_file: Path | None = None,
    invocation_context: InvocationContext | None = None,
) -> None:
    return await invoke_agent_with_runner(
        config=config,
        model=model,
        prompt=prompt,
        output_file=output_file,
        log_file=log_file,
        invocation_context=invocation_context,
        command_runner=invocation_command_module.run_command_once,
    )


async def invoke_agent_with_runner(
    config: AgentConfig,
    model: str | None,
    prompt: str,
    output_file: Path,
    log_file: Path | None,
    invocation_context: InvocationContext | None,
    command_runner: CommandRunner,
) -> None:
    return await invocation_loop_module.run_invocation_loop(
        config=config,
        model=model,
        prompt=prompt,
        output_file=output_file,
        log_file=log_file,
        invocation_context=invocation_context,
        command_runner=command_runner,
    )


class DefaultAgentInvoker:
    """Invoke provider CLIs through the default subprocess runner."""

    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        return await invoke_agent(
            config,
            model,
            prompt,
            output_file,
            log_file,
            invocation_context=invocation_context,
        )
