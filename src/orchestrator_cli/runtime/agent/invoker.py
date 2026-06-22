from __future__ import annotations

from pathlib import Path
from typing import Protocol

from orchestrator_cli.architecture.contracts import (
    ChildProcessEnvironment,
    CommandRunner,
    InvocationContext,
    InvocationPlan,
    LogPresentationDescriptor,
)
from orchestrator_cli.core.config import AgentConfig

from .invocation import command as invocation_command_module
from .invocation import loop as invocation_loop_module
from .workspace_environment import prepare_workspace_child_environment


class InvocationPlanBuilder(Protocol):
    def __call__(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
    ) -> InvocationPlan: ...


class LogPresentationBuilder(Protocol):
    def __call__(self, config: AgentConfig) -> LogPresentationDescriptor | None: ...


async def invoke_agent(
    config: AgentConfig,
    model: str | None,
    prompt: str,
    output_file: Path,
    cwd: Path,
    log_file: Path | None = None,
    invocation_context: InvocationContext | None = None,
    plan_builder: InvocationPlanBuilder | None = None,
    child_environment: ChildProcessEnvironment | None = None,
) -> None:
    if plan_builder is None:
        raise RuntimeError(
            "Agent invocation requires an explicit invocation plan builder."
        )
    return await invoke_agent_with_runner(
        config=config,
        model=model,
        prompt=prompt,
        output_file=output_file,
        cwd=cwd,
        log_file=log_file,
        invocation_context=invocation_context,
        command_runner=invocation_command_module.run_command_once,
        plan_builder=plan_builder,
        child_environment=child_environment,
    )


async def invoke_agent_with_runner(
    config: AgentConfig,
    model: str | None,
    prompt: str,
    output_file: Path,
    cwd: Path,
    log_file: Path | None,
    invocation_context: InvocationContext | None,
    command_runner: CommandRunner,
    plan_builder: InvocationPlanBuilder,
    child_environment: ChildProcessEnvironment | None = None,
) -> None:
    plan = plan_builder(config, model, prompt, output_file)
    effective_context, effective_child_environment = (
        prepare_workspace_child_environment(
            invocation_context,
            child_environment,
        )
    )
    return await invocation_loop_module.run_invocation_loop(
        config=config,
        prompt=prompt,
        output_file=output_file,
        log_file=log_file,
        cwd=cwd,
        invocation_context=effective_context,
        command_runner=command_runner,
        plan=plan,
        child_environment=effective_child_environment,
    )


class PlannedAgentInvoker:
    """Invoke provider CLIs through an adapter-supplied invocation plan."""

    def __init__(
        self,
        plan_builder: InvocationPlanBuilder,
        log_presentation_builder: LogPresentationBuilder,
    ) -> None:
        self._plan_builder = plan_builder
        self._log_presentation_builder = log_presentation_builder

    def log_presentation_for(
        self,
        config: AgentConfig,
    ) -> LogPresentationDescriptor | None:
        return self._log_presentation_builder(config)

    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        return await invoke_agent(
            config,
            model,
            prompt,
            output_file,
            cwd,
            log_file,
            invocation_context=invocation_context,
            plan_builder=self._plan_builder,
        )
