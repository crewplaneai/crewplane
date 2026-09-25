import asyncio
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime
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
from crewplane.runtime.agent.invocation.command import (
    prepare_runtime_for_attempt,
    run_invocation_attempt,
)
from crewplane.runtime.agent.invocation.output import cleanup_structured_output_file


@pytest.mark.parametrize("attempt", [0, 1, 3])
@pytest.mark.parametrize("with_context", [False, True])
def test_attempt_preserves_plan_arguments_and_exact_headers(
    tmp_path, attempt, with_context
) -> None:
    plan = replace(
        build_cli_invocation_plan(
            AgentConfig(cli_cmd=["provider"]), None, "prompt", tmp_path / "out"
        ),
        cmd=["provider", "--literal", "value with spaces"],
        stdin_data=b"prompt\x00\n",
        log_header=b"original header\r\n",
    )
    context = InvocationContext("node", "task", "provider", "executor")
    environment = ChildProcessEnvironment({"SETTING": "value"}, ("REMOVED",))
    result = CommandResult(0, "answer", "")
    runner = AsyncMock(return_value=result)
    now = datetime(2026, 9, 24, tzinfo=UTC)
    with patch("crewplane.runtime.agent.process.runner.datetime") as clock:
        clock.now.return_value = now
        actual = asyncio.run(
            run_invocation_attempt(
                plan,
                runner,
                tmp_path / "log",
                attempt,
                tmp_path,
                context if with_context else None,
                None,
                12.5,
                environment,
            )
        )
    assert actual is result
    runner.assert_awaited_once_with(
        cmd=["provider", "--literal", "value with spaces"],
        stdin_data=b"prompt\x00\n",
        log_file=tmp_path / "log",
        append_log=attempt > 0,
        log_header=(
            f"\n---\nretry_attempt: {attempt + 1}\n"
            "started_at: 2026-09-24T00:00:00+00:00\n---\n"
        ).encode()
        if attempt
        else b"original header\r\n",
        cwd=tmp_path,
        invocation_context=replace(context, attempt_num=attempt + 1)
        if with_context
        else None,
        idle_timeout_seconds=12.5,
        child_environment=environment,
    )


def test_prepare_runtime_for_attempt_clears_stale_structured_output() -> None:
    plan = build_cli_invocation_plan(
        AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            default_model="gpt-5.5",
            prompt_transport_arg="-",
        ),
        "gpt-5.5",
        "prompt",
        Path("output.txt"),
    )
    assert plan.structured_output_file is not None
    try:
        plan.structured_output_file.write_text("stale", encoding="utf-8")

        prepare_runtime_for_attempt(plan)

        assert not plan.structured_output_file.exists()
    finally:
        cleanup_structured_output_file(plan.structured_output_file)


class InvocationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_invocation_attempt_passes_idle_timeout_to_runner(self) -> None:
        observed_idle_timeouts: list[float | None] = []
        plan = build_cli_invocation_plan(
            AgentConfig(cli_cmd=[sys.executable], default_model="test"),
            "test",
            "prompt",
            Path("output.txt"),
        )

        async def runner(
            cmd: list[str],  # noqa: ARG001
            stdin_data: bytes | None,  # noqa: ARG001
            log_file: Path | None,  # noqa: ARG001
            append_log: bool,  # noqa: ARG001
            log_header: bytes | None,  # noqa: ARG001
            cwd: Path,  # noqa: ARG001
            invocation_context: InvocationContext | None,  # noqa: ARG001
            idle_timeout_seconds: float | None,
            child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
        ) -> CommandResult:
            observed_idle_timeouts.append(idle_timeout_seconds)
            return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

        result = await run_invocation_attempt(
            plan=plan,
            command_runner=runner,
            log_file=None,
            attempt=0,
            cwd=Path.cwd(),
            invocation_context=None,
            timeout_seconds=None,
            idle_timeout_seconds=12.5,
            child_environment=None,
        )

        assert result.stdout_text == "ok"
        assert observed_idle_timeouts == [12.5]

    async def test_run_invocation_attempt_emits_timeout_diagnostic(self) -> None:
        diagnostics = []
        plan = build_cli_invocation_plan(
            AgentConfig(cli_cmd=[sys.executable], default_model="test"),
            "test",
            "prompt",
            Path("output.txt"),
        )

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
            await asyncio.sleep(10)
            return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

        context = InvocationContext(
            node_id="node.a",
            task_id="generic_executor_0",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            diagnostics=diagnostics.append,
        )

        with pytest.raises(
            RuntimeError, match="wall-clock timeout reached after 0.01s"
        ):
            await run_invocation_attempt(
                plan=plan,
                command_runner=runner,
                log_file=None,
                attempt=0,
                cwd=Path.cwd(),
                invocation_context=context,
                timeout_seconds=0.01,
                idle_timeout_seconds=None,
                child_environment=None,
            )

        assert [diagnostic.operation for diagnostic in diagnostics] == [
            "invocation_timeout"
        ]
        assert diagnostics[0].attributes["timeout_scope"] == "wall_clock"
