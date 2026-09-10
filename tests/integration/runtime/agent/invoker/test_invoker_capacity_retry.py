import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.adapters.invokers.cli_invoker.capabilities import (
    CODEX_MODEL_CAPACITY_MESSAGE,
)
from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    InvocationContext,
)
from crewplane.core.config import AgentConfig
from crewplane.runtime.agent.failures import (
    InvocationFailureError,
)
from crewplane.runtime.agent.invoker import (
    invoke_agent_with_runner,
)


class InvokerRetryBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_model_capacity_retry_succeeds_on_second_attempt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            attempts = {"count": 0}

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return CommandResult(
                        returncode=1,
                        stdout_text="",
                        stderr_text=CODEX_MODEL_CAPACITY_MESSAGE,
                    )
                structured_output = Path(cmd[cmd.index("--output-last-message") + 1])
                structured_output.write_text("done", encoding="utf-8")
                return CommandResult(
                    returncode=0,
                    stdout_text='{"type":"response.completed","response":{}}',
                    stderr_text="",
                )

            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="test",
                max_retries=0,
            )
            sleep_mock = AsyncMock()
            with patch(
                "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                sleep_mock,
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="test-model",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=output_file.parent,
                    log_file=None,
                    invocation_context=None,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            assert attempts["count"] == 2
            sleep_mock.assert_awaited_once_with(5.0)
            assert output_file.read_text(encoding="utf-8") == "done"

    async def test_codex_capacity_retry_preserves_ordinary_retry_budget(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            attempts = {"count": 0}

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return CommandResult(
                        returncode=1,
                        stdout_text="",
                        stderr_text=CODEX_MODEL_CAPACITY_MESSAGE,
                    )
                if attempts["count"] == 2:
                    return CommandResult(
                        returncode=2,
                        stdout_text="",
                        stderr_text="ordinary transient failure",
                    )
                structured_output = Path(cmd[cmd.index("--output-last-message") + 1])
                structured_output.write_text("done", encoding="utf-8")
                return CommandResult(
                    returncode=0,
                    stdout_text='{"type":"response.completed","response":{}}',
                    stderr_text="",
                )

            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="test",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_exit_codes=[2],
            )
            sleep_mock = AsyncMock()
            with patch(
                "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                sleep_mock,
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="test-model",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=output_file.parent,
                    log_file=None,
                    invocation_context=None,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            assert attempts["count"] == 3
            assert [await_call.args for await_call in sleep_mock.await_args_list] == [
                (5.0,),
                (0,),
            ]
            assert output_file.read_text(encoding="utf-8") == "done"

    async def test_codex_capacity_retry_remains_available_after_ordinary_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            attempts = {"count": 0}

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return CommandResult(
                        returncode=2,
                        stdout_text="",
                        stderr_text="ordinary transient failure",
                    )
                if attempts["count"] == 2:
                    return CommandResult(
                        returncode=1,
                        stdout_text="",
                        stderr_text=CODEX_MODEL_CAPACITY_MESSAGE,
                    )
                structured_output = Path(cmd[cmd.index("--output-last-message") + 1])
                structured_output.write_text("done", encoding="utf-8")
                return CommandResult(
                    returncode=0,
                    stdout_text='{"type":"response.completed","response":{}}',
                    stderr_text="",
                )

            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="test",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_exit_codes=[2],
            )
            sleep_mock = AsyncMock()
            with patch(
                "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                sleep_mock,
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="test-model",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=output_file.parent,
                    log_file=None,
                    invocation_context=None,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            assert attempts["count"] == 3
            assert [await_call.args for await_call in sleep_mock.await_args_list] == [
                (0,),
                (5.0,),
            ]
            assert output_file.read_text(encoding="utf-8") == "done"

    async def test_codex_model_capacity_retry_stops_after_second_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            attempts = {"count": 0}

            async def runner(
                cmd: list[str],  # noqa: ARG001
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                attempts["count"] += 1
                return CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text=CODEX_MODEL_CAPACITY_MESSAGE,
                )

            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="test",
                max_retries=0,
            )
            sleep_mock = AsyncMock()
            with (
                patch(
                    "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                    sleep_mock,
                ),
                pytest.raises(InvocationFailureError) as caught,
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="test-model",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=output_file.parent,
                    log_file=None,
                    invocation_context=None,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            assert attempts["count"] == 2
            sleep_mock.assert_awaited_once_with(5.0)
            assert "Command output matched retry conditions after 1 retries" in str(
                caught.value
            )
            assert not output_file.exists()
