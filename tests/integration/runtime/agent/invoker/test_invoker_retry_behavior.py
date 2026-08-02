import asyncio
import sys
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
    invoke_agent,
    invoke_agent_with_runner,
)


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

            with pytest.MonkeyPatch.context() as process_state:
                process_state.setenv("STATE_FILE", str(state_file))
                config = AgentConfig(
                    cli_cmd=[sys.executable, str(script_path)],
                    default_model="test",
                    model_arg=None,
                    max_retries=1,
                    retry_delay_seconds=0,
                    retry_on_exit_codes=[2],
                    retry_on_stderr_contains=["temporary error"],
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
            )
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "agent.log"

            await invoke_agent(
                config,
                "test-model",
                "prompt",
                output_file,
                output_file.parent,
                log_file=log_file,
                plan_builder=build_cli_invocation_plan,
            )

            log_content = log_file.read_text(encoding="utf-8")
            self.assertIn("started_at:", log_content)
            resolved_python = Path(sys.executable).resolve(strict=True).as_posix()
            self.assertIn(f"cli_executable: {resolved_python}", log_content)
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
            )
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "agent.log"

            await invoke_agent(
                config,
                None,
                "prompt",
                output_file,
                output_file.parent,
                log_file=log_file,
                plan_builder=build_cli_invocation_plan,
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
            )
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "agent.log"

            await invoke_agent(
                config,
                "test-model",
                "prompt",
                output_file,
                output_file.parent,
                log_file=log_file,
                plan_builder=build_cli_invocation_plan,
            )

            log_content = log_file.read_text(encoding="utf-8")
            self.assertIn(
                "[stderr] Attempt 1 failed: Retrying after 1.9s...", log_content
            )
            self.assertNotIn("1852.819886ms", log_content)

    async def test_log_setup_failure_reaps_spawned_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "long_running.py"
            script_path.write_text(
                "\n".join(
                    [
                        "import time",
                        "",
                        "time.sleep(10)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            created_processes: list[asyncio.subprocess.Process] = []
            original_create_subprocess_exec = asyncio.create_subprocess_exec

            async def tracking_create_subprocess_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
                process = await original_create_subprocess_exec(*args, **kwargs)
                created_processes.append(process)
                return process

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                default_model="test-model",
                model_arg=None,
            )
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "agent.log"

            try:
                with (
                    patch(
                        "crewplane.runtime.agent.invocation.command.asyncio.create_subprocess_exec",
                        new=tracking_create_subprocess_exec,
                    ),
                    patch(
                        "crewplane.runtime.agent.invocation.command.open_log_handle",
                        side_effect=OSError("cannot open log"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "Execution error"),
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

                self.assertEqual(len(created_processes), 1)
                process = created_processes[0]
                await asyncio.wait_for(process.wait(), timeout=1.0)
                self.assertIsNotNone(process.returncode)
            finally:
                for process in created_processes:
                    if process.returncode is None:
                        process.kill()
                    await asyncio.wait_for(process.wait(), timeout=1.0)

    async def test_quota_retry_guard_stops_after_five_hours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "always_quota.py"
            script_path.write_text("print('usage limit reached')\n", encoding="utf-8")

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                default_model="test",
                model_arg=None,
                quota_reached_on_contains=["usage limit reached"],
                quota_reached_retry_delay_seconds=0,
            )
            output_file = tmp_path / "output.txt"

            with (
                patch(
                    "crewplane.runtime.agent.invocation.retry.quota_retry_guard_exhausted",
                    side_effect=[False, True],
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "Quota retry guard exceeded after 5 hours",
                ) as caught,
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

            with pytest.MonkeyPatch.context() as process_state:
                process_state.setenv("STATE_FILE", str(state_file))
                config = AgentConfig(
                    cli_cmd=[sys.executable, str(script_path)],
                    default_model="test",
                    model_arg=None,
                    max_retries=1,
                    retry_delay_seconds=0,
                    retry_on_output_contains=["temporary error"],
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
                max_retries=0,
                retry_delay_seconds=0,
                retry_on_output_contains=["temporary error"],
            )
            output_file = tmp_path / "output.txt"

            with self.assertRaisesRegex(RuntimeError, "matched retry conditions"):
                await invoke_agent(
                    config,
                    "test-model",
                    "prompt",
                    output_file,
                    output_file.parent,
                    plan_builder=build_cli_invocation_plan,
                )
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
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
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
                cwd=output_file.parent,
                log_file=None,
                invocation_context=None,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )
            self.assertEqual(attempts["count"], 2)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "done")

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

            self.assertEqual(attempts["count"], 2)
            sleep_mock.assert_awaited_once_with(5.0)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "done")

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

            self.assertEqual(attempts["count"], 3)
            self.assertEqual(
                [await_call.args for await_call in sleep_mock.await_args_list],
                [(5.0,), (0,)],
            )
            self.assertEqual(output_file.read_text(encoding="utf-8"), "done")

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

            self.assertEqual(attempts["count"], 3)
            self.assertEqual(
                [await_call.args for await_call in sleep_mock.await_args_list],
                [(0,), (5.0,)],
            )
            self.assertEqual(output_file.read_text(encoding="utf-8"), "done")

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
                self.assertRaises(InvocationFailureError) as caught,
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
            sleep_mock.assert_awaited_once_with(5.0)
            self.assertIn(
                "Command output matched retry conditions after 1 retries",
                str(caught.exception),
            )
            self.assertFalse(output_file.exists())

    async def test_configured_failed_exit_retry_exhaustion_preserves_exit_failure(
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
                    returncode=2,
                    stdout_text="",
                    stderr_text="temporary error",
                )

            config = AgentConfig(
                cli_cmd=["provider"],
                default_model="test",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_exit_codes=[2],
            )
            sleep_mock = AsyncMock()
            with (
                patch(
                    "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                    sleep_mock,
                ),
                self.assertRaises(InvocationFailureError) as caught,
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
            sleep_mock.assert_awaited_once_with(0)
            failure_message = str(caught.exception)
            self.assertIn("Exit code 2", failure_message)
            self.assertNotIn("matched retry conditions", failure_message)
            self.assertFalse(output_file.exists())

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
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
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
                provider_kind="gemini",
                quota_reached_retry_delay_seconds=0,
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
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
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
                provider_kind="gemini",
                quota_reached_retry_delay_seconds=0,
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
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
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
                provider_kind="copilot",
                default_model="test-model",
                quota_reached_on_contains=["rate limit", "quota", "too many requests"],
                quota_reached_retry_delay_seconds=0,
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
                cwd: Path,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by callback or protocol signature.
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
                provider_kind="copilot",
                default_model="test-model",
                quota_reached_on_contains=["rate limit", "quota", "too many requests"],
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
            self.assertAlmostEqual(float(sleep_mock.await_args.args[0]), 3.0, delta=0.2)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "ok")
