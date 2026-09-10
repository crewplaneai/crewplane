import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    InvocationContext,
    InvocationDiagnostic,
)
from crewplane.core.config import AgentConfig
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invocation.command import run_command_once
from crewplane.runtime.agent.invoker import invoke_agent_with_runner


class InvocationLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_receive_incrementing_process_attempt_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            observed_attempts: list[int] = []

            async def runner(
                cmd: list[str],  # noqa: ARG001
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,
                idle_timeout_seconds: float | None,  # noqa: ARG001
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                assert invocation_context is not None
                observed_attempts.append(invocation_context.attempt_num)
                output = "temporary error" if len(observed_attempts) == 1 else "done"
                return CommandResult(returncode=0, stdout_text=output, stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role=ProviderRole.EXECUTOR,
            )
            config = AgentConfig(
                cli_cmd=["provider"],
                provider_kind="generic",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_output_contains=["temporary error"],
            )

            await invoke_agent_with_runner(
                config=config,
                model=None,
                prompt="prompt",
                output_file=output_file,
                cwd=output_file.parent,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            assert observed_attempts == [1, 2]
            assert output_file.read_text(encoding="utf-8") == "done"

    async def test_completion_buffered_output_disables_idle_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            diagnostics: list[InvocationDiagnostic] = []
            context = InvocationContext(
                node_id="node.a",
                task_id="gemini_executor_0",
                provider="gemini",
                role=ProviderRole.EXECUTOR,
                diagnostics=diagnostics.append,
            )
            terminal_response = '{"response":"done","stats":{"models":{}}}'
            config = AgentConfig(
                cli_cmd=[
                    sys.executable,
                    "-c",
                    f"import time; time.sleep(0.1); print({terminal_response!r})",
                ],
                provider_kind="gemini",
                invocation_timeout_seconds=1,
                invocation_idle_timeout_seconds=0.02,
            )

            await invoke_agent_with_runner(
                config=config,
                model=None,
                prompt="prompt",
                output_file=output_file,
                cwd=tmp_path,
                log_file=None,
                invocation_context=context,
                command_runner=run_command_once,
                plan_builder=build_cli_invocation_plan,
            )

            assert output_file.read_text(encoding="utf-8") == "done"
            idle_timeout_diagnostics = [
                diagnostic
                for diagnostic in diagnostics
                if diagnostic.operation == "invocation_idle_timeout_unavailable"
            ]
            assert len(idle_timeout_diagnostics) == 1
            assert idle_timeout_diagnostics[0].attributes == {
                "configured_idle_timeout_seconds": 0.02
            }

    async def test_completion_buffered_output_preserves_wall_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            diagnostics: list[InvocationDiagnostic] = []
            context = InvocationContext(
                node_id="node.a",
                task_id="gemini_executor_0",
                provider="gemini",
                role=ProviderRole.EXECUTOR,
                diagnostics=diagnostics.append,
            )
            terminal_response = '{"response":"done","stats":{"models":{}}}'
            config = AgentConfig(
                cli_cmd=[
                    sys.executable,
                    "-c",
                    f"import time; time.sleep(0.2); print({terminal_response!r})",
                ],
                provider_kind="gemini",
                invocation_timeout_seconds=0.05,
                invocation_idle_timeout_seconds=0.01,
            )

            with pytest.raises(
                RuntimeError, match="wall-clock timeout reached after 0.05s"
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model=None,
                    prompt="prompt",
                    output_file=tmp_path / "output.txt",
                    cwd=tmp_path,
                    log_file=None,
                    invocation_context=context,
                    command_runner=run_command_once,
                    plan_builder=build_cli_invocation_plan,
                )

            assert [diagnostic.operation for diagnostic in diagnostics] == [
                "invocation_idle_timeout_unavailable",
                "invocation_timeout",
            ]

    async def test_structured_output_file_is_precleared_before_every_attempt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            attempts = 0
            observed_missing_before_write = []

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                nonlocal attempts
                attempts += 1
                output_path = Path(cmd[cmd.index("--output-last-message") + 1])
                observed_missing_before_write.append(not output_path.exists())
                output_path.write_text(
                    "retry marker" if attempts == 1 else "final",
                    encoding="utf-8",
                )
                return CommandResult(
                    returncode=0,
                    stdout_text='{"type":"response.completed","response":{}}',
                    stderr_text="",
                )

            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.5",
                prompt_transport="stdin",
                prompt_transport_arg="-",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_output_contains=["retry marker"],
            )
            sleep_mock = AsyncMock()
            with patch(
                "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                sleep_mock,
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="gpt-5.5",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=tmp_path,
                    log_file=None,
                    invocation_context=None,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            assert attempts == 2
            assert observed_missing_before_write == [True, True]
            assert output_file.read_text(encoding="utf-8") == "final"
