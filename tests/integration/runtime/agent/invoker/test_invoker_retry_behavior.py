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

            assert output_file.read_text().strip() == "success"
            assert state_file.read_text().strip() == "2"

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
                pytest.raises(
                    RuntimeError, match="Quota retry guard exceeded after 5 hours"
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

            assert isinstance(caught.value, InvocationFailureError)
            failure = caught.value
            assert isinstance(failure, InvocationFailureError)
            assert failure.kind == "quota_or_rate_limit"
            assert failure.phase == "provider_transport"
            assert not output_file.exists()

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

            assert output_file.read_text().strip() == "success"
            assert state_file.read_text().strip() == "2"

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

            with pytest.raises(RuntimeError, match="matched retry conditions"):
                await invoke_agent(
                    config,
                    "test-model",
                    "prompt",
                    output_file,
                    output_file.parent,
                    plan_builder=build_cli_invocation_plan,
                )
            assert not output_file.exists()

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
            assert attempts["count"] == 2
            assert output_file.read_text(encoding="utf-8") == "done"

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
            sleep_mock.assert_awaited_once_with(0)
            failure_message = str(caught.value)
            assert "Exit code 2" in failure_message
            assert "matched retry conditions" not in failure_message
            assert not output_file.exists()
