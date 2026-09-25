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


@pytest.mark.parametrize("phase", ["environment", "usage_state", "idle_timeout"])
def test_plan_output_is_owned_during_runtime_setup(
    tmp_path, monkeypatch, phase
) -> None:
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from crewplane.runtime.agent import invoker
    from crewplane.runtime.agent.invocation import loop

    owned = tmp_path / "owned"
    config = AgentConfig(cli_cmd=["provider"])
    plan = replace(
        build_cli_invocation_plan(config, None, "prompt", tmp_path / "out"),
        structured_output_file=owned,
    )
    owned.write_text("allocated")

    def fail_setup(*args, **kwargs):
        assert args or kwargs
        raise RuntimeError("setup failed")

    def build_plan(*args):
        assert args[-1] == tmp_path
        return plan

    if phase == "environment":
        monkeypatch.setattr(invoker, "prepare_workspace_child_environment", fail_setup)
    elif phase == "usage_state":
        monkeypatch.setattr(loop, "InvocationUsageAccumulator", fail_setup)
    else:
        config.invocation_idle_timeout_seconds = 1
        plan = replace(plan, supports_output_idle_timeout=False)
        monkeypatch.setattr(loop, "emit_invocation_diagnostic", fail_setup)
    runner = AsyncMock()
    with pytest.raises(RuntimeError, match="setup failed"):
        asyncio.run(
            invoke_agent_with_runner(
                config,
                None,
                "prompt",
                tmp_path / "out",
                tmp_path,
                None,
                None,
                runner,
                build_plan,
            )
        )
    runner.assert_not_called()
    assert not owned.exists()


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize(
    "outcome", ["success", "publication_failure", "retry", "cancel"]
)
def test_extracted_file_lifetime_across_attempt_and_outer_cleanup(
    tmp_path, monkeypatch, owned, outcome
):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from crewplane.architecture.contracts import OutputExtractionResult
    from crewplane.runtime.agent.invocation import loop

    extracted_path = tmp_path / "extracted.txt"
    output_file = tmp_path / "final.md"
    if outcome == "publication_failure":
        output_file.mkdir()
    config = AgentConfig(
        cli_cmd=["provider"],
        max_retries=1,
        retry_delay_seconds=0,
        retry_on_output_contains=["retry marker"],
    )
    attempts = 0

    def capture(**kwargs):
        nonlocal attempts
        assert kwargs["append_log"] is (attempts > 0)
        assert not extracted_path.exists()
        attempts += 1
        text = (
            "retry marker"
            if outcome in {"retry", "cancel"} and attempts == 1
            else "café 🌍"
        )
        extracted_path.write_text(text, encoding="utf-8")
        return CommandResult(0, "", "")

    def extract(result, structured_file):
        assert result.returncode == 0
        assert structured_file == (None if owned else extracted_path)
        return OutputExtractionResult(
            "",
            "success",
            extracted_path,
            len(extracted_path.read_text(encoding="utf-8")),
            owned,
        )

    async def sleep(delay):
        assert delay == 0
        assert extracted_path.read_text(encoding="utf-8") == "retry marker"
        if outcome == "cancel":
            raise asyncio.CancelledError

    original_cleanup = loop.cleanup_structured_output_file

    def outer_cleanup(path):
        assert path == (None if owned else extracted_path)
        assert extracted_path.exists() is (not owned)
        original_cleanup(path)

    monkeypatch.setattr(loop.asyncio, "sleep", sleep)
    monkeypatch.setattr(loop, "cleanup_structured_output_file", outer_cleanup)
    plan = replace(
        build_cli_invocation_plan(config, None, "prompt", output_file),
        output_extractor=extract,
        structured_output_file=None if owned else extracted_path,
    )
    invocation = loop.run_invocation_loop(
        config,
        "prompt",
        output_file,
        None,
        tmp_path,
        None,
        AsyncMock(side_effect=capture),
        plan,
    )
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(invocation)
    elif outcome == "publication_failure":
        with pytest.raises(IsADirectoryError):
            asyncio.run(invocation)
    else:
        asyncio.run(invocation)
        assert output_file.read_bytes() == "café 🌍".encode()
    assert attempts == (2 if outcome == "retry" else 1)
    assert not extracted_path.exists()
