import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
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
    invoke_agent,
    invoke_agent_with_runner,
)


class QuotaRetrySubprocessTests(unittest.IsolatedAsyncioTestCase):
    async def test_quota_retries_until_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_file = tmp_path / "state.txt"
            script_path = tmp_path / "quota_script.py"

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
                        "if count < 3:",
                        "    print('usage limit reached')",
                        "    sys.exit(0)",
                        "print('ok')",
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
                    quota_reached_on_contains=["usage limit reached"],
                    quota_reached_retry_delay_seconds=0,
                )
                output_file = tmp_path / "output.txt"
                await invoke_agent(
                    config,
                    "test-model",
                    "prompt",
                    output_file,
                    output_file.parent,
                    plan_builder=build_cli_invocation_plan,
                )
            finally:
                if original_state is None:
                    os.environ.pop("STATE_FILE", None)
                else:
                    os.environ["STATE_FILE"] = original_state

            self.assertEqual(output_file.read_text().strip(), "ok")
            self.assertEqual(state_file.read_text().strip(), "3")

    async def test_quota_retry_uses_parsed_reset_with_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_file = tmp_path / "state.txt"
            script_path = tmp_path / "gemini_quota_once.py"
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
                        "    print('You have exhausted your capacity on this model. Your quota will reset after 2s.')",
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
                    provider_kind="gemini",
                    quota_reached_retry_delay_seconds=0,
                    quota_reset_sleep_floor_seconds=5,
                )
                output_file = tmp_path / "output.txt"
                sleep_mock = AsyncMock()
                with patch(
                    "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                    sleep_mock,
                ):
                    await invoke_agent(
                        config,
                        "test-model",
                        "prompt",
                        output_file,
                        output_file.parent,
                        plan_builder=build_cli_invocation_plan,
                    )
            finally:
                if original_state is None:
                    os.environ.pop("STATE_FILE", None)
                else:
                    os.environ["STATE_FILE"] = original_state

            self.assertEqual(output_file.read_text(encoding="utf-8").strip(), "ok")
            self.assertEqual(state_file.read_text(encoding="utf-8").strip(), "2")
            self.assertEqual(sleep_mock.await_count, 1)
            sleep_seconds = float(sleep_mock.await_args.args[0])
            self.assertAlmostEqual(sleep_seconds, 7.0, delta=0.2)

    async def test_quota_retry_respects_configured_minimum_delay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_file = tmp_path / "state.txt"
            script_path = tmp_path / "gemini_quota_once_min_delay.py"
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
                        "    print('You have exhausted your capacity on this model. Your quota will reset after 2s.')",
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
                    provider_kind="gemini",
                    quota_reached_retry_delay_seconds=30,
                    quota_reset_sleep_floor_seconds=5,
                )
                output_file = tmp_path / "output.txt"
                sleep_mock = AsyncMock()
                with patch(
                    "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                    sleep_mock,
                ):
                    await invoke_agent(
                        config,
                        "test-model",
                        "prompt",
                        output_file,
                        output_file.parent,
                        plan_builder=build_cli_invocation_plan,
                    )
            finally:
                if original_state is None:
                    os.environ.pop("STATE_FILE", None)
                else:
                    os.environ["STATE_FILE"] = original_state

            self.assertEqual(output_file.read_text(encoding="utf-8").strip(), "ok")
            self.assertEqual(state_file.read_text(encoding="utf-8").strip(), "2")
            self.assertEqual(sleep_mock.await_count, 1)
            sleep_seconds = float(sleep_mock.await_args.args[0])
            self.assertAlmostEqual(sleep_seconds, 30.0, delta=0.2)

    async def test_quota_retry_fails_fast_when_parsed_reset_exceeds_five_hours(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "gemini_long_quota.py"
            script_path.write_text(
                "print('You have exhausted your capacity on this model. Your quota will reset after 6h.')\n",
                encoding="utf-8",
            )

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                default_model="test",
                model_arg=None,
                provider_kind="gemini",
                quota_reached_retry_delay_seconds=0,
                quota_reset_sleep_floor_seconds=5,
            )
            output_file = tmp_path / "output.txt"
            sleep_mock = AsyncMock()
            with (
                patch(
                    "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                    sleep_mock,
                ),
                self.assertRaisesRegex(RuntimeError, "exceeds 5 hours") as caught,
            ):
                await invoke_agent(
                    config,
                    "test-model",
                    "prompt",
                    output_file,
                    output_file.parent,
                    plan_builder=build_cli_invocation_plan,
                )
            self.assertIsInstance(caught.exception, InvocationFailureError)
            failure = caught.exception
            assert isinstance(failure, InvocationFailureError)
            self.assertEqual(failure.kind, "quota_or_rate_limit")
            self.assertEqual(failure.phase, "provider_transport")
            self.assertFalse(output_file.exists())
            self.assertEqual(sleep_mock.await_count, 0)

    async def test_gemini_internal_quota_exhaustion_retries_outer_window(
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
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                attempts["count"] += 1
                if attempts["count"] > 1:
                    return CommandResult(returncode=0, stdout_text="ok", stderr_text="")
                return CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text=(
                        "Attempt 3 failed: You have exhausted your capacity on this model. "
                        "Your quota will reset after 1s.. Max attempts reached\n"
                        "RetryableQuotaError: You have exhausted your capacity on this model."
                    ),
                )

            config = AgentConfig(
                cli_cmd=["gemini"],
                provider_kind="gemini",
                default_model="test-model",
                quota_reached_retry_delay_seconds=300,
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

            self.assertEqual(attempts["count"], 2)
            self.assertEqual(sleep_mock.await_count, 1)
            self.assertEqual(float(sleep_mock.await_args.args[0]), 300.0)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "ok")

    async def test_gemini_attempt_lines_retry_outer_window_without_legacy_marker(
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
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                attempts["count"] += 1
                if attempts["count"] > 1:
                    return CommandResult(returncode=0, stdout_text="ok", stderr_text="")
                return CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text=(
                        "Attempt 1 failed: You have exhausted your capacity on this model. "
                        "Retrying after 900ms...\n"
                        "Attempt 2 failed: You have exhausted your capacity on this model. "
                        "Retrying after 10000ms..."
                    ),
                )

            config = AgentConfig(
                cli_cmd=["gemini"],
                provider_kind="gemini",
                default_model="test-model",
                quota_reached_retry_delay_seconds=0,
                quota_reset_sleep_floor_seconds=0,
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

            self.assertEqual(attempts["count"], 2)
            self.assertEqual(sleep_mock.await_count, 1)
            self.assertAlmostEqual(
                float(sleep_mock.await_args.args[0]), 10.0, delta=0.1
            )
            self.assertEqual(output_file.read_text(encoding="utf-8"), "ok")

    async def test_gemini_single_attempt_retry_line_uses_outer_retry(self) -> None:
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
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                attempts["count"] += 1
                if attempts["count"] > 1:
                    return CommandResult(returncode=0, stdout_text="ok", stderr_text="")
                return CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text=(
                        "Attempt 1 failed: You have exhausted your capacity on this model. "
                        "Your quota will reset after 0s.. Retrying after 840.730092ms..."
                    ),
                )

            config = AgentConfig(
                cli_cmd=["gemini"],
                provider_kind="gemini",
                default_model="test-model",
                quota_reached_retry_delay_seconds=0,
                quota_reset_sleep_floor_seconds=0,
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

            self.assertEqual(attempts["count"], 2)
            self.assertEqual(sleep_mock.await_count, 1)
            self.assertAlmostEqual(
                float(sleep_mock.await_args.args[0]), 0.840730092, delta=0.001
            )
            self.assertEqual(output_file.read_text(encoding="utf-8"), "ok")

    async def test_gemini_capacity_error_log_retries_with_fixed_delay(self) -> None:
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
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                attempts["count"] += 1
                if attempts["count"] > 1:
                    return CommandResult(returncode=0, stdout_text="ok", stderr_text="")
                return CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text=(
                        "YOLO mode is enabled. All tool calls will be automatically approved.\n"
                        "Loaded cached credentials.\n"
                        "Attempt 1 failed with status 429. Retrying with backoff...\n"
                        '{\n  "error": {\n    "code": 429,\n    "status": "RESOURCE_EXHAUSTED"\n  }\n}\n'
                        "Attempt 10 failed: No capacity available for model gemini-3.1-pro-preview on the server. Max attempts reached\n"
                        "RetryableQuotaError: No capacity available for model gemini-3.1-pro-preview on the server"
                    ),
                )

            config = AgentConfig(
                cli_cmd=["gemini"],
                provider_kind="gemini",
                default_model="test-model",
                quota_reached_retry_delay_seconds=300,
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

            self.assertEqual(attempts["count"], 2)
            self.assertEqual(sleep_mock.await_count, 1)
            self.assertEqual(float(sleep_mock.await_args.args[0]), 300.0)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "ok")

    async def test_quota_retry_uses_fixed_delay_when_reset_not_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_file = tmp_path / "state.txt"
            script_path = tmp_path / "quota_no_reset_once.py"
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
                        "    print('usage limit reached')",
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
                    quota_reached_on_contains=["usage limit reached"],
                    quota_reached_retry_delay_seconds=13,
                )
                output_file = tmp_path / "output.txt"
                sleep_mock = AsyncMock()
                with patch(
                    "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                    sleep_mock,
                ):
                    await invoke_agent(
                        config,
                        "test-model",
                        "prompt",
                        output_file,
                        output_file.parent,
                        plan_builder=build_cli_invocation_plan,
                    )
            finally:
                if original_state is None:
                    os.environ.pop("STATE_FILE", None)
                else:
                    os.environ["STATE_FILE"] = original_state

            self.assertEqual(output_file.read_text(encoding="utf-8").strip(), "ok")
            self.assertEqual(sleep_mock.await_count, 1)
            self.assertEqual(float(sleep_mock.await_args.args[0]), 13.0)

    async def test_quota_retry_guard_rejects_wait_that_crosses_five_hours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "quota_short_reset.py"
            script_path.write_text(
                "print('You have exhausted your capacity on this model. Your quota will reset after 4s.')\n",
                encoding="utf-8",
            )

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                default_model="test",
                model_arg=None,
                provider_kind="gemini",
                quota_reached_retry_delay_seconds=0,
                quota_reset_sleep_floor_seconds=5,
            )
            output_file = tmp_path / "output.txt"
            sleep_mock = AsyncMock()
            with (
                patch(
                    "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                    sleep_mock,
                ),
                patch(
                    "crewplane.runtime.agent.invocation.retry.quota_retry_guard_exhausted",
                    return_value=False,
                ),
                patch(
                    "crewplane.runtime.agent.invocation.retry.quota_retry_guard_will_exhaust",
                    return_value=True,
                ),
                self.assertRaisesRegex(RuntimeError, "would exceed 5 hours"),
            ):
                await invoke_agent(
                    config,
                    "test-model",
                    "prompt",
                    output_file,
                    output_file.parent,
                    plan_builder=build_cli_invocation_plan,
                )
            self.assertFalse(output_file.exists())
            self.assertEqual(sleep_mock.await_count, 0)
