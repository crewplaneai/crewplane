import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.failures import (
    InvocationFailureError,
)
from orchestrator_cli.runtime.agent.invoker import (
    invoke_agent,
    invoke_agent_with_runner,
)
from orchestrator_cli.runtime.agent.types import CommandResult, InvocationContext


class InvokerRetryBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_on_exit_code_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_file = tmp_path / "state.txt"
            script_path = tmp_path / "retry_script.py"

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
                        "    print('temporary error', file=sys.stderr)",
                        "    sys.exit(2)",
                        "print('success')",
                        "",
                    ]
                )
            )

            original_state = os.environ.get("STATE_FILE")
            os.environ["STATE_FILE"] = str(state_file)
            try:
                config = AgentConfig(
                    cli_cmd=[sys.executable, str(script_path)],
                    default_model="test",
                    model_arg=None,
                    prompt_arg=None,
                    max_retries=1,
                    retry_delay_seconds=0,
                    retry_on_exit_codes=[2],
                    retry_on_stderr_contains=["temporary error"],
                )
                output_file = tmp_path / "output.txt"
                await invoke_agent(config, "test-model", "prompt", output_file)
            finally:
                if original_state is None:
                    os.environ.pop("STATE_FILE", None)
                else:
                    os.environ["STATE_FILE"] = original_state

            self.assertEqual(output_file.read_text().strip(), "success")
            self.assertEqual(state_file.read_text().strip(), "2")

    async def test_log_file_includes_header_and_stream_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "log_script.py"
            script_path.write_text(
                "\n".join(
                    [
                        "import sys",
                        "print('stdout line')",
                        "print('stderr line', file=sys.stderr)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                default_model="test-model",
                model_arg=None,
                prompt_arg=None,
            )
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "agent.log"

            await invoke_agent(
                config,
                "test-model",
                "prompt",
                output_file,
                log_file=log_file,
            )

            log_content = log_file.read_text(encoding="utf-8")
            self.assertIn("started_at:", log_content)
            self.assertIn(f"cli_executable: {sys.executable}", log_content)
            self.assertIn("model: test-model", log_content)
            self.assertIn(f"output_file: {output_file}", log_content)
            self.assertIn("---", log_content)
            self.assertIn("stdout line", log_content)
            self.assertIn("[stderr] stderr line", log_content)

    async def test_log_file_uses_provider_default_label_when_model_is_omitted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "log_script.py"
            script_path.write_text(
                "print('stdout line')\n",
                encoding="utf-8",
            )

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                model_arg=None,
                prompt_arg=None,
            )
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "agent.log"

            await invoke_agent(
                config,
                None,
                "prompt",
                output_file,
                log_file=log_file,
            )

            log_content = log_file.read_text(encoding="utf-8")
            self.assertIn("model: provider default", log_content)

    async def test_log_file_normalizes_retry_wait_milliseconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "log_retry_units_script.py"
            script_path.write_text(
                "\n".join(
                    [
                        "import sys",
                        "print('ok')",
                        "print('Attempt 1 failed: Retrying after 1852.819886ms...', file=sys.stderr)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                default_model="test-model",
                model_arg=None,
                prompt_arg=None,
            )
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "agent.log"

            await invoke_agent(
                config,
                "test-model",
                "prompt",
                output_file,
                log_file=log_file,
            )

            log_content = log_file.read_text(encoding="utf-8")
            self.assertIn(
                "[stderr] Attempt 1 failed: Retrying after 1.9s...", log_content
            )
            self.assertNotIn("1852.819886ms", log_content)

    async def test_log_setup_failure_reaps_spawned_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            marker_path = tmp_path / "marker.txt"
            script_path = tmp_path / "long_running.py"
            script_path.write_text(
                "\n".join(
                    [
                        "import os",
                        "import time",
                        "from pathlib import Path",
                        "",
                        "marker = Path(os.environ['MARKER_PATH'])",
                        "time.sleep(0.3)",
                        "marker.write_text('child survived', encoding='utf-8')",
                        "time.sleep(3)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            original_marker = os.environ.get("MARKER_PATH")
            os.environ["MARKER_PATH"] = str(marker_path)
            try:
                config = AgentConfig(
                    cli_cmd=[sys.executable, str(script_path)],
                    default_model="test-model",
                    model_arg=None,
                    prompt_arg=None,
                )
                output_file = tmp_path / "output.txt"
                log_file = tmp_path / "agent.log"

                with (
                    patch(
                        "orchestrator_cli.runtime.agent.invocation.command.open_log_handle",
                        side_effect=OSError("cannot open log"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "Execution error"),
                ):
                    await invoke_agent(
                        config,
                        "test-model",
                        "prompt",
                        output_file,
                        log_file=log_file,
                    )

                await asyncio.sleep(0.6)
                self.assertFalse(marker_path.exists())
            finally:
                if original_marker is None:
                    os.environ.pop("MARKER_PATH", None)
                else:
                    os.environ["MARKER_PATH"] = original_marker

    async def test_quota_retry_guard_stops_after_five_hours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "always_quota.py"
            script_path.write_text("print('usage limit reached')\n", encoding="utf-8")

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                default_model="test",
                model_arg=None,
                prompt_arg=None,
                quota_reached_on_contains=["usage limit reached"],
                quota_reached_retry_delay_seconds=0,
            )
            output_file = tmp_path / "output.txt"

            with (
                patch(
                    "orchestrator_cli.runtime.agent.invocation.retry.quota_retry_guard_exhausted",
                    side_effect=[False, True],
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "Quota retry guard exceeded after 5 hours",
                ) as caught,
            ):
                await invoke_agent(config, "test-model", "prompt", output_file)

            self.assertIsInstance(caught.exception, InvocationFailureError)
            failure = caught.exception
            assert isinstance(failure, InvocationFailureError)
            self.assertEqual(failure.kind, "quota_or_rate_limit")
            self.assertEqual(failure.phase, "provider_transport")
            self.assertFalse(output_file.exists())

    async def test_retries_on_output_contains_when_exit_code_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_file = tmp_path / "state.txt"
            script_path = tmp_path / "output_retry_script.py"

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
                        "    print('temporary error')",
                        "    sys.exit(0)",
                        "print('success')",
                        "",
                    ]
                )
            )

            original_state = os.environ.get("STATE_FILE")
            os.environ["STATE_FILE"] = str(state_file)
            try:
                config = AgentConfig(
                    cli_cmd=[sys.executable, str(script_path)],
                    default_model="test",
                    model_arg=None,
                    prompt_arg=None,
                    max_retries=1,
                    retry_delay_seconds=0,
                    retry_on_output_contains=["temporary error"],
                )
                output_file = tmp_path / "output.txt"
                await invoke_agent(config, "test-model", "prompt", output_file)
            finally:
                if original_state is None:
                    os.environ.pop("STATE_FILE", None)
                else:
                    os.environ["STATE_FILE"] = original_state

            self.assertEqual(output_file.read_text().strip(), "success")
            self.assertEqual(state_file.read_text().strip(), "2")

    async def test_raises_when_retry_condition_matches_with_no_retries_left(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "always_error.py"
            script_path.write_text(
                "\n".join(
                    [
                        "import sys",
                        "print('temporary error')",
                        "sys.exit(0)",
                        "",
                    ]
                )
            )

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                default_model="test",
                model_arg=None,
                prompt_arg=None,
                max_retries=0,
                retry_delay_seconds=0,
                retry_on_output_contains=["temporary error"],
            )
            output_file = tmp_path / "output.txt"

            with self.assertRaisesRegex(
                RuntimeError, "matched configured retry conditions"
            ):
                await invoke_agent(config, "test-model", "prompt", output_file)
            self.assertFalse(output_file.exists())

    async def test_invoke_agent_with_runner_retries_and_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            attempts = {"count": 0}

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return CommandResult(
                        returncode=0, stdout_text="temporary error", stderr_text=""
                    )
                return CommandResult(returncode=0, stdout_text="done", stderr_text="")

            config = AgentConfig(
                cli_cmd=["echo"],
                default_model="test",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_output_contains=["temporary error"],
            )
            await invoke_agent_with_runner(
                config=config,
                model="test-model",
                prompt="prompt",
                output_file=output_file,
                log_file=None,
                invocation_context=None,
                command_runner=runner,
            )
            self.assertEqual(attempts["count"], 2)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "done")

    async def test_invoke_agent_with_runner_ignores_stderr_quota_when_stdout_present(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            attempts = {"count": 0}

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                attempts["count"] += 1
                return CommandResult(
                    returncode=0,
                    stdout_text="review complete",
                    stderr_text=(
                        "Attempt 1 failed: You have exhausted your capacity on this model. "
                        "Your quota will reset after 1s."
                    ),
                )

            config = AgentConfig(
                cli_cmd=["echo"],
                default_model="test",
                quota_parser="gemini",
                quota_reached_retry_delay_seconds=0,
            )
            await invoke_agent_with_runner(
                config=config,
                model="test-model",
                prompt="prompt",
                output_file=output_file,
                log_file=None,
                invocation_context=None,
                command_runner=runner,
            )
            self.assertEqual(attempts["count"], 1)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "review complete")

    async def test_invoke_agent_with_runner_retries_stderr_quota_when_stdout_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            attempts = {"count": 0}

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return CommandResult(
                        returncode=0,
                        stdout_text="",
                        stderr_text=(
                            "You have exhausted your capacity on this model. "
                            "Your quota will reset after 1s."
                        ),
                    )
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            config = AgentConfig(
                cli_cmd=["echo"],
                default_model="test",
                quota_parser="gemini",
                quota_reached_retry_delay_seconds=0,
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
                    log_file=None,
                    invocation_context=None,
                    command_runner=runner,
                )
            self.assertEqual(attempts["count"], 2)
            self.assertEqual(sleep_mock.await_count, 1)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "ok")

    async def test_copilot_successful_report_with_quota_prose_is_not_retried(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            attempts = {"count": 0}

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                attempts["count"] += 1
                return CommandResult(
                    returncode=0,
                    stdout_text=(
                        "Final report: retry policy, quota handling, and "
                        "rate limit handling should stay in the runtime invoker. "
                        "Retry after 3s examples are documentation, not provider errors."
                    ),
                    stderr_text="",
                )

            config = AgentConfig(
                cli_cmd=["copilot"],
                default_model="test-model",
                quota_parser="copilot",
                quota_reached_on_contains=["rate limit", "quota", "too many requests"],
                quota_reached_retry_delay_seconds=0,
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
                    log_file=None,
                    invocation_context=None,
                    command_runner=runner,
                )

            self.assertEqual(attempts["count"], 1)
            self.assertEqual(sleep_mock.await_count, 0)
            self.assertIn(
                "quota handling",
                output_file.read_text(encoding="utf-8"),
            )

    async def test_copilot_quota_error_still_retries_with_broad_legacy_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            attempts = {"count": 0}

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return CommandResult(
                        returncode=0,
                        stdout_text="quota reached, retry after 3s",
                        stderr_text="",
                    )
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            config = AgentConfig(
                cli_cmd=["copilot"],
                default_model="test-model",
                quota_parser="copilot",
                quota_reached_on_contains=["rate limit", "quota", "too many requests"],
                quota_reached_retry_delay_seconds=0,
                quota_reset_sleep_floor_seconds=0,
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
                    log_file=None,
                    invocation_context=None,
                    command_runner=runner,
                )

            self.assertEqual(attempts["count"], 2)
            self.assertEqual(sleep_mock.await_count, 1)
            self.assertAlmostEqual(float(sleep_mock.await_args.args[0]), 3.0, delta=0.2)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "ok")
