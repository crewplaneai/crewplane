import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.invocation.command import run_command_once
from orchestrator_cli.runtime.agent.invoker import (
    DefaultAgentInvoker,
    invoke_agent,
    invoke_agent_with_runner,
)
from orchestrator_cli.runtime.agent.types import InvocationContext


class InvokerFacadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_invoke_agent_delegates_to_runner_facade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            delegated = AsyncMock()

            with patch(
                "orchestrator_cli.runtime.agent.invoker.invoke_agent_with_runner",
                delegated,
            ):
                await invoke_agent(
                    config=AgentConfig(cli_cmd=["echo"], default_model="test"),
                    model="test",
                    prompt="prompt",
                    output_file=output_file,
                )

            delegated.assert_awaited_once()
            assert delegated.await_args.kwargs["command_runner"] is run_command_once

    async def test_invoke_agent_with_runner_delegates_to_invocation_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            runner = AsyncMock()
            delegated = AsyncMock()

            with patch(
                "orchestrator_cli.runtime.agent.invocation.loop.run_invocation_loop",
                delegated,
            ):
                await invoke_agent_with_runner(
                    config=AgentConfig(cli_cmd=["echo"], default_model="test"),
                    model="test",
                    prompt="prompt",
                    output_file=output_file,
                    log_file=None,
                    invocation_context=None,
                    command_runner=runner,
                )

            delegated.assert_awaited_once()
            assert delegated.await_args.kwargs["command_runner"] is runner

    async def test_default_agent_invoker_delegates_to_invoke_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            delegated = AsyncMock()
            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role="executor",
            )

            with patch(
                "orchestrator_cli.runtime.agent.invoker.invoke_agent",
                delegated,
            ):
                await DefaultAgentInvoker().invoke(
                    config=AgentConfig(cli_cmd=["echo"], default_model="test"),
                    model="test",
                    prompt="prompt",
                    output_file=output_file,
                    invocation_context=context,
                )

            delegated.assert_awaited_once()
            assert delegated.await_args.kwargs["invocation_context"] is context
