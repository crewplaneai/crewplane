import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestrator_cli.adapters.invokers.cli_invoker import build_cli_invocation_plan
from orchestrator_cli.architecture.contracts import CommandResult, InvocationContext
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.failures import InvocationFailureError
from orchestrator_cli.runtime.agent.invocation.command import run_command_once
from orchestrator_cli.runtime.agent.invoker import invoke_agent_with_runner
from orchestrator_cli.runtime.agent.process import stream_capture
from orchestrator_cli.runtime.agent.usage import InvocationUsage, estimate_token_count


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
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
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
                role="executor",
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
            )

            with self.assertRaises(asyncio.CancelledError):
                await invoke_agent_with_runner(
                    config=config,
                    model="gpt-5.4",
                    prompt="prompt",
                    output_file=output_file,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            assert structured_output_path is not None
            self.assertFalse(structured_output_path.exists())
            self.assertEqual(usages, [])
            self.assertFalse(output_file.exists())

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
                    "orchestrator_cli.runtime.agent.process.stream_capture.tempfile.mkstemp",
                    side_effect=recording_mkstemp,
                ),
                self.assertRaises(InvocationFailureError),
            ):
                await invoke_agent_with_runner(
                    config=AgentConfig(
                        cli_cmd=[sys.executable, "-c", "raise ValueError('fail')"],
                        default_model="test",
                    ),
                    model="test",
                    prompt="prompt",
                    output_file=Path(tmp_dir) / "output.txt",
                    log_file=Path(tmp_dir) / "agent.log",
                    invocation_context=None,
                    command_runner=run_command_once,
                    plan_builder=build_cli_invocation_plan,
                )

            for path in created_paths:
                self.assertFalse(path.exists())

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
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
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
                role="executor",
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_output_contains=["retry marker"],
            )

            with (
                patch(
                    "orchestrator_cli.runtime.agent.invocation.loop.asyncio.sleep",
                    new=cancel_sleep,
                ),
                self.assertRaises(asyncio.CancelledError),
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="gpt-5.4",
                    prompt="prompt",
                    output_file=output_file,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            assert structured_output_path is not None
            self.assertFalse(structured_output_path.exists())
            self.assertEqual(usages, [])
            self.assertFalse(output_file.exists())

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
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
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
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_output_contains=["retry marker"],
            )
            sleep_mock = AsyncMock()
            with patch(
                "orchestrator_cli.runtime.agent.invocation.loop.asyncio.sleep",
                sleep_mock,
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="gpt-5.4",
                    prompt="prompt",
                    output_file=output_file,
                    log_file=None,
                    invocation_context=None,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertEqual(attempts, 2)
            self.assertEqual(observed_missing_before_write, [True, True])
            self.assertEqual(output_file.read_text(encoding="utf-8"), "final")

    async def test_success_records_usage_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            usages = []

            async def runner(
                cmd: list[str],  # noqa: ARG001
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role="executor",
                usage_recorder=usages.append,
            )
            await invoke_agent_with_runner(
                config=AgentConfig(cli_cmd=["echo"], default_model="test"),
                model="test",
                prompt="prompt",
                output_file=output_file,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            self.assertEqual(len(usages), 1)

    async def test_visible_output_streams_persisted_stdout_to_final_artifact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            stream_path = tmp_path / "captured-stdout.txt"
            output_text = "line 1\n" + ("x" * 10_000)
            stream_path.write_text(output_text, encoding="utf-8")
            usages: list[InvocationUsage] = []

            async def runner(
                cmd: list[str],  # noqa: ARG001
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001
            ) -> CommandResult:
                return CommandResult(
                    returncode=0,
                    stdout_text="x" * 32,
                    stderr_text="",
                    stdout_path=stream_path,
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role="executor",
                usage_recorder=usages.append,
            )
            await invoke_agent_with_runner(
                config=AgentConfig(cli_cmd=["echo"], default_model="test"),
                model="test",
                prompt="prompt",
                output_file=output_file,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            self.assertEqual(output_file.read_text(encoding="utf-8"), output_text)
            self.assertFalse(stream_path.exists())
            self.assertEqual(len(usages), 1)
            self.assertEqual(
                usages[0].visible_estimate_tokens,
                estimate_token_count(len("prompt"))
                + estimate_token_count(len(output_text)),
            )

    async def test_structured_provider_retry_records_usage_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            usages: list[InvocationUsage] = []

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                output_path = Path(cmd[cmd.index("--output-last-message") + 1])
                output_text = "retry" if not append_log else "f"
                output_path.write_text(output_text, encoding="utf-8")
                return CommandResult(
                    returncode=0,
                    stdout_text='{"type":"response.completed","response":{}}',
                    stderr_text="",
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="codex_executor_0",
                provider="codex",
                role="executor",
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_output_contains=["retry"],
            )

            await invoke_agent_with_runner(
                config=config,
                model="gpt-5.4",
                prompt="",
                output_file=output_file,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            self.assertEqual(len(usages), 1)
            usage = usages[0]
            self.assertEqual(usage.visible_estimate_tokens, 2)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "f")

    async def test_failure_records_usage_once_before_reraising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            usages = []

            async def runner(
                cmd: list[str],  # noqa: ARG001
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                return CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text="fatal",
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role="executor",
                usage_recorder=usages.append,
            )
            with self.assertRaises(InvocationFailureError):
                await invoke_agent_with_runner(
                    config=AgentConfig(cli_cmd=["echo"], default_model="test"),
                    model="test",
                    prompt="prompt",
                    output_file=output_file,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertEqual(len(usages), 1)

    async def test_quota_failure_records_usage_once_before_reraising(self) -> None:
        quota_message = (
            "You have exhausted your capacity on this model. "
            "Your quota will reset after 6h."
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            usages = []

            async def runner(
                cmd: list[str],  # noqa: ARG001
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                return CommandResult(
                    returncode=0,
                    stdout_text=quota_message,
                    stderr_text="",
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="gemini",
                role="executor",
                usage_recorder=usages.append,
            )
            with self.assertRaises(InvocationFailureError):
                await invoke_agent_with_runner(
                    config=AgentConfig(
                        cli_cmd=["gemini"],
                        provider_kind="gemini",
                        default_model="test",
                        model_arg=None,
                        quota_reached_retry_delay_seconds=0,
                        quota_reset_sleep_floor_seconds=5,
                    ),
                    model="test",
                    prompt="prompt",
                    output_file=output_file,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertEqual(len(usages), 1)
            self.assertEqual(
                usages[0].visible_estimate_tokens,
                estimate_token_count(len("prompt"))
                + estimate_token_count(len(quota_message)),
            )
            self.assertFalse(output_file.exists())
