import os
import sys
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import AsyncMock, patch

import pytest

from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    InvocationContext,
)
from crewplane.core.config import AgentConfig
from crewplane.runtime.agent.invoker import (
    invoke_agent,
    invoke_agent_with_runner,
)
from tests.helpers.fixture_files import copy_fixture

pytestmark = pytest.mark.usefixtures("posix_cli_plans")


class QuotaRetrySubprocessTests(unittest.IsolatedAsyncioTestCase):
    @pytest.fixture(autouse=True)
    def temporary_directory_root(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    async def test_quota_retries_until_clear(self) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
        tmp_path = Path(tmp_dir)
        state_file = tmp_path / "state.txt"
        script_path = copy_fixture(
            "providers/quota_until_clear.py", tmp_path / "quota_script.py"
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
        tmp_dir = mkdtemp(dir=self.tmp_path)
        tmp_path = Path(tmp_dir)
        state_file = tmp_path / "state.txt"
        script_path = copy_fixture(
            "providers/gemini_quota_once.py", tmp_path / "gemini_quota_once.py"
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
                "crewplane.runtime.agent.invocation.loop.sleep",
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

    async def test_gemini_internal_quota_exhaustion_retries_outer_window(
        self,
    ) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
                return CommandResult(
                    returncode=0,
                    stdout_text='{"response":"ok"}',
                    stderr_text="",
                )
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
            "crewplane.runtime.agent.invocation.loop.sleep",
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
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
                return CommandResult(
                    returncode=0,
                    stdout_text='{"response":"ok"}',
                    stderr_text="",
                )
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
            "crewplane.runtime.agent.invocation.loop.sleep",
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
        self.assertAlmostEqual(float(sleep_mock.await_args.args[0]), 10.0, delta=0.1)
        self.assertEqual(output_file.read_text(encoding="utf-8"), "ok")

    async def test_gemini_single_attempt_retry_line_uses_outer_retry(self) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
                return CommandResult(
                    returncode=0,
                    stdout_text='{"response":"ok"}',
                    stderr_text="",
                )
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
            "crewplane.runtime.agent.invocation.loop.sleep",
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
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
                return CommandResult(
                    returncode=0,
                    stdout_text='{"response":"ok"}',
                    stderr_text="",
                )
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
            "crewplane.runtime.agent.invocation.loop.sleep",
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
