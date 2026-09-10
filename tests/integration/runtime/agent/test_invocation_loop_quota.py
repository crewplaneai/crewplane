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
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.failures import InvocationFailureError
from crewplane.runtime.agent.invoker import invoke_agent_with_runner
from crewplane.runtime.agent.usage import estimate_token_count


class InvocationLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_quota_wait_ceiling_counts_only_completed_quota_sleeps(
        self,
    ) -> None:
        quota_result = CommandResult(
            returncode=1,
            stdout_text="You have exhausted your capacity on this model.",
            stderr_text="",
        )
        runner = AsyncMock(side_effect=[quota_result, quota_result, quota_result])
        sleep = AsyncMock()

        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch("crewplane.runtime.agent.invocation.loop.asyncio.sleep", sleep),
            patch(
                "crewplane.runtime.agent.invocation.retry.quota_retry_elapsed_seconds",
                return_value=20,
            ),
            pytest.raises(InvocationFailureError) as caught,
        ):
            await invoke_agent_with_runner(
                config=AgentConfig(
                    cli_cmd=["gemini"],
                    provider_kind="gemini",
                    default_model="test",
                    model_arg=None,
                    quota_reached_retry_delay_seconds=10,
                    quota_retry_max_wait_seconds=25,
                ),
                model="test",
                prompt="prompt",
                output_file=Path(tmp_dir) / "output.txt",
                cwd=Path(tmp_dir),
                log_file=None,
                invocation_context=None,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

        assert runner.await_count == 3
        assert sleep.await_count == 2
        assert [item.args[0] for item in sleep.await_args_list] == [10, 10]
        assert "cumulative quota wait is 20s" in str(caught.value)

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
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
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
                role=ProviderRole.EXECUTOR,
                usage_recorder=usages.append,
            )
            with pytest.raises(InvocationFailureError):
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
                    cwd=Path(tmp_dir),
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            assert len(usages) == 1
            assert usages[0].visible_estimate_tokens == estimate_token_count(
                len("prompt")
            ) + estimate_token_count(len(quota_message))
            assert not output_file.exists()

    async def test_quota_failure_preserves_last_non_quota_failure(self) -> None:
        results = [
            CommandResult(returncode=1, stdout_text="", stderr_text="fatal transport"),
            CommandResult(
                returncode=0,
                stdout_text="partial success",
                stderr_text="HTTP 429 Too Many Requests",
            ),
            CommandResult(
                returncode=1,
                stdout_text="Quota reached. Your quota will reset after 6h.",
                stderr_text="",
            ),
        ]

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
            return results.pop(0)

        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            pytest.raises(InvocationFailureError) as caught,
        ):
            await invoke_agent_with_runner(
                config=AgentConfig(
                    cli_cmd=["provider"],
                    max_retries=2,
                    retry_delay_seconds=0,
                    retry_on_exit_codes=[1],
                    retry_on_stderr_contains=["HTTP 429"],
                    quota_reached_retry_delay_seconds=0,
                ),
                model=None,
                prompt="prompt",
                output_file=Path(tmp_dir) / "output.txt",
                cwd=Path(tmp_dir),
                log_file=None,
                invocation_context=None,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

        last_failure = caught.value.last_non_quota_failure
        assert last_failure is not None
        assert last_failure is not None
        assert "fatal transport" in last_failure.message
        assert "last distinct non-quota failure: fatal transport" in str(caught.value)
        assert any(
            "Last distinct non-quota failure" in note for note in caught.value.__notes__
        )

    async def test_quota_classified_retry_is_not_saved_as_non_quota_failure(
        self,
    ) -> None:
        results = [
            CommandResult(
                returncode=0,
                stdout_text="partial success",
                stderr_text="HTTP 429 Too Many Requests",
            ),
            CommandResult(
                returncode=1,
                stdout_text="Quota reached. Your quota will reset after 6h.",
                stderr_text="",
            ),
        ]

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
            return results.pop(0)

        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            pytest.raises(InvocationFailureError) as caught,
        ):
            await invoke_agent_with_runner(
                config=AgentConfig(
                    cli_cmd=["provider"],
                    max_retries=1,
                    retry_delay_seconds=0,
                    retry_on_stderr_contains=["HTTP 429"],
                    quota_reached_retry_delay_seconds=0,
                ),
                model=None,
                prompt="prompt",
                output_file=Path(tmp_dir) / "output.txt",
                cwd=Path(tmp_dir),
                log_file=None,
                invocation_context=None,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

        assert caught.value.last_non_quota_failure is None
        assert "last distinct non-quota failure" not in str(caught.value)
        assert not any(
            "Last distinct non-quota failure" in note
            for note in getattr(caught.value, "__notes__", ())
        )
