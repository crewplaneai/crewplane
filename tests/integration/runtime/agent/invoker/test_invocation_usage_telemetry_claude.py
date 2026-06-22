import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.adapters.invokers.cli_invoker import build_cli_invocation_plan
from orchestrator_cli.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    InvocationContext,
)
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.failures import (
    InvocationFailureError,
)
from orchestrator_cli.runtime.agent.invoker import (
    invoke_agent_with_runner,
)


class InvocationUsageTelemetryClaudeTests(unittest.IsolatedAsyncioTestCase):
    async def test_invoke_agent_with_runner_enforces_claude_structured_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []
            captured: dict[str, bytes | list[str] | None] = {}

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                captured["cmd"] = cmd
                captured["stdin_data"] = stdin_data
                return CommandResult(
                    returncode=0,
                    stdout_text='{"result":"done","usage":{"input_tokens":120,"output_tokens":30}}',
                    stderr_text="",
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="claude_executor_0",
                provider="claude",
                role="executor",
                round_num=1,
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["./claude"],
                provider_kind="claude",
                default_model="sonnet",
                pricing={"input": 3.0, "output": 15.0},
            )
            await invoke_agent_with_runner(
                config=config,
                model="sonnet",
                prompt="review the repository",
                output_file=output_file,
                cwd=output_file.parent,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            self.assertEqual(
                captured["cmd"],
                [
                    "./claude",
                    "--model",
                    "sonnet",
                    "--output-format",
                    "json",
                ],
            )
            self.assertEqual(captured["stdin_data"], b"review the repository")
            self.assertEqual(output_file.read_text(encoding="utf-8"), "done")
            self.assertEqual(usages[0].provider_usage_status, "full")

    async def test_invoke_agent_with_runner_treats_claude_usage_parse_failure_as_telemetry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                return CommandResult(
                    returncode=0,
                    stdout_text='{"result":"done","usage":{"input_tokens":"bad"}}',
                    stderr_text="",
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="claude_executor_0",
                provider="claude",
                role="executor",
                round_num=1,
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["./claude"],
                provider_kind="claude",
                default_model="sonnet",
            )
            await invoke_agent_with_runner(
                config=config,
                model="sonnet",
                prompt="review the repository",
                output_file=output_file,
                cwd=output_file.parent,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            self.assertEqual(output_file.read_text(encoding="utf-8"), "done")
            self.assertEqual(usages[0].provider_usage_status, "malformed")
            self.assertIn("input", usages[0].usage_parse_error or "")

    async def test_invoke_agent_with_runner_fails_when_claude_result_is_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                return CommandResult(
                    returncode=0,
                    stdout_text='{"usage":{"input_tokens":120,"output_tokens":30}}',
                    stderr_text="",
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="claude_executor_0",
                provider="claude",
                role="executor",
                round_num=1,
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["./claude"],
                provider_kind="claude",
                default_model="sonnet",
            )
            with self.assertRaisesRegex(
                RuntimeError, "claude output extraction failed: missing"
            ) as caught:
                await invoke_agent_with_runner(
                    config=config,
                    model="sonnet",
                    prompt="review the repository",
                    output_file=output_file,
                    cwd=output_file.parent,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )
            self.assertIsInstance(caught.exception, InvocationFailureError)
            failure = caught.exception
            assert isinstance(failure, InvocationFailureError)
            self.assertEqual(failure.kind, "malformed_provider_output")

            self.assertEqual(usages[0].output_extraction_status, "missing")

    async def test_invoke_agent_with_runner_tracks_stderr_fallback_usage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                return CommandResult(
                    returncode=0,
                    stdout_text="",
                    stderr_text="payload on stderr",
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="alpha_executor_0",
                provider="alpha",
                role="executor",
                round_num=1,
                usage_recorder=usages.append,
            )
            config = AgentConfig(cli_cmd=["echo"], default_model="test")
            await invoke_agent_with_runner(
                config=config,
                model="test-model",
                prompt="prompt",
                output_file=output_file,
                cwd=output_file.parent,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            self.assertEqual(
                output_file.read_text(encoding="utf-8"), "payload on stderr"
            )
            self.assertEqual(len(usages), 1)
            self.assertEqual(usages[0].visible_estimate_tokens, 7)

    async def test_invoke_agent_with_runner_records_usage_before_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                return CommandResult(returncode=1, stdout_text="", stderr_text="boom")

            context = InvocationContext(
                node_id="node.a",
                task_id="alpha_executor_0",
                provider="alpha",
                role="executor",
                round_num=1,
                usage_recorder=usages.append,
            )
            config = AgentConfig(cli_cmd=["echo"], default_model="test")
            with self.assertRaisesRegex(RuntimeError, "Exit code 1"):
                await invoke_agent_with_runner(
                    config=config,
                    model="test-model",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=output_file.parent,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertEqual(len(usages), 1)
            self.assertEqual(usages[0].attempt_count, 1)
            self.assertEqual(usages[0].cli_captured, False)
            self.assertEqual(usages[0].visible_estimate_tokens, 3)

    async def test_invoke_agent_with_runner_counts_failed_stdout_and_stderr_exactly(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                return CommandResult(
                    returncode=1,
                    stdout_text="out",
                    stderr_text="err",
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="alpha_executor_0",
                provider="alpha",
                role="executor",
                round_num=1,
                usage_recorder=usages.append,
            )
            config = AgentConfig(cli_cmd=["echo"], default_model="test")
            with self.assertRaisesRegex(RuntimeError, "Exit code 1"):
                await invoke_agent_with_runner(
                    config=config,
                    model="test-model",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=output_file.parent,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertEqual(len(usages), 1)
            self.assertFalse(usages[0].cli_captured)
            self.assertEqual(usages[0].visible_estimate_tokens, 4)
