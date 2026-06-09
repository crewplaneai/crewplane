import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator_cli.adapters.invokers.cli_invoker import build_cli_invocation_plan
from orchestrator_cli.architecture.contracts import CommandResult, InvocationContext
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.invocation.command import (
    build_invocation_runtime,
    cleanup_structured_output_file,
    prepare_runtime_for_attempt,
    run_command_once,
    run_invocation_attempt,
)


def test_prepare_runtime_for_attempt_clears_stale_structured_output() -> None:
    plan = build_cli_invocation_plan(
        AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            default_model="gpt-5.4",
            prompt_transport_arg="-",
        ),
        "gpt-5.4",
        "prompt",
        Path("output.txt"),
    )
    runtime = build_invocation_runtime(plan)
    assert runtime.structured_output_file is not None
    try:
        runtime.structured_output_file.write_text("stale", encoding="utf-8")

        prepare_runtime_for_attempt(runtime)

        assert not runtime.structured_output_file.exists()
    finally:
        cleanup_structured_output_file(runtime.structured_output_file)


class InvocationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_command_once_uses_spawned_process_group_id(self) -> None:
        if os.name != "posix":
            self.skipTest("process group lookup is POSIX-only")

        observed_pids: list[int] = []

        def fake_getpgid(pid: int) -> int:
            observed_pids.append(pid)
            return pid

        with patch(
            "orchestrator_cli.runtime.agent.invocation.command.os.getpgid",
            side_effect=fake_getpgid,
        ):
            result = await run_command_once(
                cmd=[sys.executable, "-c", "print('ok')"],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                invocation_context=None,
                idle_timeout_seconds=None,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout_text.strip(), "ok")
        self.assertEqual(len(observed_pids), 1)
        self.assertGreater(observed_pids[0], 0)

    async def test_run_invocation_attempt_passes_idle_timeout_to_runner(self) -> None:
        observed_idle_timeouts: list[float | None] = []
        plan = build_cli_invocation_plan(
            AgentConfig(cli_cmd=["tool"], default_model="test"),
            "test",
            "prompt",
            Path("output.txt"),
        )
        runtime = build_invocation_runtime(plan)

        async def runner(
            cmd: list[str],  # noqa: ARG001
            stdin_data: bytes | None,  # noqa: ARG001
            log_file: Path | None,  # noqa: ARG001
            append_log: bool,  # noqa: ARG001
            log_header: bytes | None,  # noqa: ARG001
            invocation_context: InvocationContext | None,  # noqa: ARG001
            idle_timeout_seconds: float | None,
        ) -> CommandResult:
            observed_idle_timeouts.append(idle_timeout_seconds)
            return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

        result = await run_invocation_attempt(
            runtime=runtime,
            command_runner=runner,
            log_file=None,
            attempt=0,
            invocation_context=None,
            timeout_seconds=None,
            idle_timeout_seconds=12.5,
        )

        self.assertEqual(result.stdout_text, "ok")
        self.assertEqual(observed_idle_timeouts, [12.5])

    async def test_run_invocation_attempt_emits_timeout_diagnostic(self) -> None:
        diagnostics = []
        plan = build_cli_invocation_plan(
            AgentConfig(cli_cmd=["tool"], default_model="test"),
            "test",
            "prompt",
            Path("output.txt"),
        )
        runtime = build_invocation_runtime(plan)

        async def runner(
            cmd: list[str],  # noqa: ARG001
            stdin_data: bytes | None,  # noqa: ARG001
            log_file: Path | None,  # noqa: ARG001
            append_log: bool,  # noqa: ARG001
            log_header: bytes | None,  # noqa: ARG001
            invocation_context: InvocationContext | None,  # noqa: ARG001
            idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
        ) -> CommandResult:
            await asyncio.sleep(10)
            return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

        context = InvocationContext(
            node_id="node.a",
            task_id="generic_executor_0",
            provider="generic",
            role="executor",
            diagnostics=diagnostics.append,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "wall-clock timeout reached after 0.01s",
        ):
            await run_invocation_attempt(
                runtime=runtime,
                command_runner=runner,
                log_file=None,
                attempt=0,
                invocation_context=context,
                timeout_seconds=0.01,
                idle_timeout_seconds=None,
            )

        self.assertEqual(
            [diagnostic.operation for diagnostic in diagnostics], ["invocation_timeout"]
        )
        self.assertEqual(diagnostics[0].attributes["timeout_scope"], "wall_clock")
