import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    InvocationContext,
)
from crewplane.core.config import AgentConfig
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.failures import InvocationFailureError
from crewplane.runtime.agent.invocation.command import run_command_once
from crewplane.runtime.agent.invoker import invoke_agent_with_runner
from crewplane.runtime.agent.process import stream_capture


class InvocationLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_during_command_cleans_structured_output_without_usage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []
            structured_output_path: Path | None = None

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
                nonlocal structured_output_path
                structured_output_path = Path(
                    cmd[cmd.index("--output-last-message") + 1]
                )
                structured_output_path.write_text("partial", encoding="utf-8")
                raise asyncio.CancelledError

            context = InvocationContext(
                node_id="node.a",
                task_id="codex_executor_0",
                provider="codex",
                role=ProviderRole.EXECUTOR,
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.5",
                prompt_transport="stdin",
                prompt_transport_arg="-",
            )

            with pytest.raises(asyncio.CancelledError):
                await invoke_agent_with_runner(
                    config=config,
                    model="gpt-5.5",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=tmp_path,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            assert structured_output_path is not None
            assert not structured_output_path.exists()
            assert usages == []
            assert not output_file.exists()

    async def test_command_stream_capture_files_are_cleaned_between_attempts(
        self,
    ) -> None:
        created_paths: list[Path] = []
        original_mkstemp = stream_capture.tempfile.mkstemp

        def recording_mkstemp(*args, **kwargs):
            fd, raw_path = original_mkstemp(*args, **kwargs)
            created_paths.append(Path(raw_path))
            return fd, raw_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            with (
                patch(
                    "crewplane.runtime.agent.process.stream_capture.tempfile.mkstemp",
                    side_effect=recording_mkstemp,
                ),
                pytest.raises(InvocationFailureError),
            ):
                await invoke_agent_with_runner(
                    config=AgentConfig(
                        cli_cmd=[sys.executable, "-c", "raise ValueError('fail')"],
                        default_model="test",
                    ),
                    model="test",
                    prompt="prompt",
                    output_file=Path(tmp_dir) / "output.txt",
                    cwd=Path(tmp_dir),
                    log_file=Path(tmp_dir) / "agent.log",
                    invocation_context=None,
                    command_runner=run_command_once,
                    plan_builder=build_cli_invocation_plan,
                )

            for path in created_paths:
                assert not path.exists()

    async def test_cancellation_during_retry_sleep_cleans_structured_output_without_usage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []
            structured_output_path: Path | None = None

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
                nonlocal structured_output_path
                structured_output_path = Path(
                    cmd[cmd.index("--output-last-message") + 1]
                )
                structured_output_path.write_text("retry marker", encoding="utf-8")
                return CommandResult(
                    returncode=0,
                    stdout_text='{"type":"response.completed","response":{}}',
                    stderr_text="",
                )

            async def cancel_sleep(wait_seconds: float) -> None:  # noqa: ARG001
                raise asyncio.CancelledError

            context = InvocationContext(
                node_id="node.a",
                task_id="codex_executor_0",
                provider="codex",
                role=ProviderRole.EXECUTOR,
                usage_recorder=usages.append,
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

            with (
                patch(
                    "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                    new=cancel_sleep,
                ),
                pytest.raises(asyncio.CancelledError),
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="gpt-5.5",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=tmp_path,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            assert structured_output_path is not None
            assert not structured_output_path.exists()
            assert usages == []
            assert not output_file.exists()
