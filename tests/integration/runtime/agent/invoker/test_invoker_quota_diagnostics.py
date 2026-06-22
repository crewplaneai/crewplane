import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestrator_cli.adapters.invokers.cli_invoker import build_cli_invocation_plan
from orchestrator_cli.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    InvocationContext,
)
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.invoker import (
    invoke_agent,
    invoke_agent_with_runner,
)


class InvokerQuotaDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_quota_retry_appends_attempt_header_to_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_file = tmp_path / "state.txt"
            script_path = tmp_path / "copilot_quota_once.py"
            script_path.write_text(
                "\n".join(
                    [
                        "import os",
                        "import sys",
                        "from pathlib import Path",
                        "",
                        "state_path = Path(os.environ['STATE_FILE'])",
                        "count = int(state_path.read_text()) if state_path.exists() else 0",
                        "count += 1",
                        "state_path.write_text(str(count))",
                        "if count < 2:",
                        "    print('quota reached, retry after 0s')",
                        "    sys.exit(0)",
                        "print('ok')",
                    ]
                ),
                encoding="utf-8",
            )

            original_state = os.environ.get("STATE_FILE")
            os.environ["STATE_FILE"] = str(state_file)
            try:
                config = AgentConfig(
                    cli_cmd=[sys.executable, str(script_path)],
                    default_model="test",
                    model_arg=None,
                    provider_kind="copilot",
                    quota_reached_on_contains=[
                        "rate limit",
                        "quota",
                        "too many requests",
                    ],
                    quota_reached_retry_delay_seconds=0,
                    quota_reset_sleep_floor_seconds=0,
                )
                output_file = tmp_path / "output.txt"
                log_file = tmp_path / "copilot.log"
                sleep_mock = AsyncMock()
                with patch(
                    "orchestrator_cli.runtime.agent.invocation.loop.asyncio.sleep",
                    sleep_mock,
                ):
                    await invoke_agent(
                        config,
                        "test-model",
                        "prompt",
                        output_file,
                        output_file.parent,
                        log_file=log_file,
                        plan_builder=build_cli_invocation_plan,
                    )
            finally:
                if original_state is None:
                    os.environ.pop("STATE_FILE", None)
                else:
                    os.environ["STATE_FILE"] = original_state

            log_text = log_file.read_text(encoding="utf-8")
            self.assertEqual(output_file.read_text(encoding="utf-8").strip(), "ok")
            self.assertIn("cli_executable:", log_text)
            self.assertIn("retry_attempt: 2", log_text)
            self.assertIn("quota reached, retry after 0s", log_text)
            self.assertIn("ok", log_text)

    async def test_invoke_agent_with_runner_uses_stderr_when_stdout_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"

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

            config = AgentConfig(
                cli_cmd=["echo"],
                default_model="test",
            )
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
            self.assertEqual(
                output_file.read_text(encoding="utf-8"),
                "payload on stderr",
            )

    async def test_invoke_agent_with_runner_emits_stderr_fallback_diagnostic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            diagnostics = []

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
                diagnostics=diagnostics.append,
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

            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0].operation, "stderr_fallback")
            self.assertEqual(diagnostics[0].level, "warning")
            self.assertIn(
                "invocation succeeded with empty stdout",
                diagnostics[0].message,
            )

    async def test_invoke_agent_with_runner_emits_retry_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            diagnostics = []
            attempts = {"count": 0}

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
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return CommandResult(
                        returncode=1, stdout_text="", stderr_text="retry me"
                    )
                return CommandResult(returncode=0, stdout_text="done", stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="alpha_executor_0",
                provider="alpha",
                role="executor",
                round_num=1,
                diagnostics=diagnostics.append,
            )
            config = AgentConfig(
                cli_cmd=["echo"],
                default_model="test",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_exit_codes=[1],
            )
            sleep_mock = AsyncMock()
            with patch(
                "orchestrator_cli.runtime.agent.invocation.loop.asyncio.sleep",
                sleep_mock,
            ):
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

            self.assertEqual(attempts["count"], 2)
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0].operation, "retry_scheduled")
            self.assertEqual(diagnostics[0].attributes["retry_count"], 1)

    async def test_invoke_agent_with_runner_emits_quota_retry_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            diagnostics = []
            attempts = {"count": 0}

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
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return CommandResult(
                        returncode=0,
                        stdout_text="You have exhausted your capacity on this model. Your quota will reset after 2s.",
                        stderr_text="",
                    )
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="alpha_executor_0",
                provider="alpha",
                role="executor",
                round_num=1,
                diagnostics=diagnostics.append,
            )
            config = AgentConfig(
                cli_cmd=["gemini"],
                provider_kind="gemini",
                default_model="test-model",
                quota_reached_retry_delay_seconds=0,
                quota_reset_sleep_floor_seconds=5,
            )
            sleep_mock = AsyncMock()
            with patch(
                "orchestrator_cli.runtime.agent.invocation.loop.asyncio.sleep",
                sleep_mock,
            ):
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

            self.assertEqual(attempts["count"], 2)
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0].operation, "quota_retry_scheduled")
            self.assertGreaterEqual(
                float(diagnostics[0].attributes["wait_seconds"]), 7.0
            )
