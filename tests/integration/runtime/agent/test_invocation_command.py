import asyncio
import unittest
from pathlib import Path

from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.invocation.command import (
    build_invocation_runtime,
    cleanup_structured_output_file,
    prepare_runtime_for_attempt,
    run_invocation_attempt,
)
from orchestrator_cli.runtime.agent.types import CommandResult, InvocationContext


def test_prepare_runtime_for_attempt_clears_stale_structured_output() -> None:
    runtime = build_invocation_runtime(
        config=AgentConfig(
            cli_cmd=["codex", "exec"],
            default_model="gpt-5.4",
            use_stdin=True,
            stdin_prompt_arg="-",
        ),
        model="gpt-5.4",
        prompt="prompt",
        output_file=Path("output.txt"),
    )
    assert runtime.structured_output_file is not None
    try:
        runtime.structured_output_file.write_text("stale", encoding="utf-8")

        prepare_runtime_for_attempt(runtime)

        assert not runtime.structured_output_file.exists()
    finally:
        cleanup_structured_output_file(runtime.structured_output_file)


class InvocationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_invocation_attempt_passes_idle_timeout_to_runner(self) -> None:
        observed_idle_timeouts: list[float | None] = []
        runtime = build_invocation_runtime(
            config=AgentConfig(cli_cmd=["tool"], default_model="test"),
            model="test",
            prompt="prompt",
            output_file=Path("output.txt"),
        )

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
        runtime = build_invocation_runtime(
            config=AgentConfig(cli_cmd=["tool"], default_model="test"),
            model="test",
            prompt="prompt",
            output_file=Path("output.txt"),
        )

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

        with self.assertRaisesRegex(RuntimeError, "timed out after 0.01s"):
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
