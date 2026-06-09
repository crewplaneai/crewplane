import asyncio
import os
import sys
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestrator_cli.adapters.invokers.cli_invoker import build_cli_invocation_plan
from orchestrator_cli.architecture.contracts import CommandResult, InvocationContext
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.invoker import (
    invoke_agent,
    invoke_agent_with_runner,
)
from orchestrator_cli.runtime.agent.usage import parse_provider_usage


class InvocationUsageTelemetryCodexTests(unittest.IsolatedAsyncioTestCase):
    async def test_invoke_agent_with_runner_records_estimated_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                return CommandResult(returncode=0, stdout_text="done", stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="alpha_executor_0",
                provider="alpha",
                role="executor",
                round_num=1,
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["echo"],
                default_model="test",
                pricing={"input": 1.0, "output": 2.0},
            )
            await invoke_agent_with_runner(
                config=config,
                model="test-model",
                prompt="prompt",
                output_file=output_file,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            self.assertEqual(len(usages), 1)
            usage = usages[0]
            self.assertEqual(usage.attempt_count, 1)
            self.assertTrue(usage.cli_captured)
            self.assertEqual(usage.output_extraction_status, "success")
            self.assertEqual(usage.provider_usage_status, "none")
            self.assertEqual(usage.provider_tokens["input"], None)
            self.assertEqual(usage.visible_estimate_tokens, 3)
            self.assertEqual(usage.invocation_cost_confidence, "partial")
            self.assertAlmostEqual(usage.configured_cost_usd or 0.0, 0.000004)

    async def test_invoke_agent_with_runner_records_retry_aware_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []
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
                        returncode=1,
                        stdout_text="",
                        stderr_text="retry me",
                    )
                return CommandResult(returncode=0, stdout_text="done", stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="alpha_executor_0",
                provider="alpha",
                role="executor",
                round_num=1,
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["echo"],
                default_model="test",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_exit_codes=[1],
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
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertEqual(len(usages), 1)
            usage = usages[0]
            self.assertEqual(usage.attempt_count, 2)
            self.assertEqual(usage.visible_estimate_tokens, 6)
            self.assertEqual(usage.provider_usage_status, "none")

    async def test_invoke_agent_with_runner_parses_codex_jsonl_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []
            payload = "\n".join(
                [
                    '{"type":"response.output_text.delta","delta":"done"}',
                    (
                        '{"type":"response.completed","response":'
                        '{"usage":{"input_tokens":120,"output_tokens":30}}}'
                    ),
                ]
            )

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                output_path = Path(cmd[cmd.index("--output-last-message") + 1])
                output_path.write_text("done", encoding="utf-8")
                return CommandResult(returncode=0, stdout_text=payload, stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="codex_executor_0",
                provider="codex",
                role="executor",
                round_num=1,
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
                pricing={"input": 1.5, "output": 6.0},
            )
            await invoke_agent_with_runner(
                config=config,
                model="gpt-5.4",
                prompt="review the repository",
                output_file=output_file,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            self.assertEqual(len(usages), 1)
            usage = usages[0]
            self.assertEqual(usage.provider_tokens["input"], 120)
            self.assertEqual(usage.provider_tokens["output"], 30)
            self.assertEqual(usage.provider_usage_status, "full")
            self.assertEqual(usage.output_extraction_status, "success")
            self.assertAlmostEqual(usage.configured_cost_usd or 0.0, 0.00036)

    async def test_invoke_agent_finalizes_codex_output_when_child_keeps_stdio_open(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "provider.log"
            child_pid_file = tmp_path / "child.pid"
            fake_codex = tmp_path / "codex"
            fake_codex.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import os",
                        "import subprocess",
                        "import sys",
                        "from pathlib import Path",
                        "",
                        "output_path = Path(sys.argv[sys.argv.index('--output-last-message') + 1])",
                        "output_path.write_text('final answer', encoding='utf-8')",
                        'print(\'{"type":"response.completed","response":{}}\', flush=True)',
                        "stdout_fd = os.dup(1)",
                        "stderr_fd = os.dup(2)",
                        "os.set_inheritable(stdout_fd, True)",
                        "os.set_inheritable(stderr_fd, True)",
                        (
                            "child = subprocess.Popen("
                            "[sys.executable, '-c', 'import time; time.sleep(30)'], "
                            "stdout=stdout_fd, stderr=stderr_fd"
                            ")"
                        ),
                        (
                            f"Path({str(child_pid_file)!r}).write_text("
                            "str(child.pid), encoding='utf-8')"
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            diagnostics = []
            config = AgentConfig(
                cli_cmd=[str(fake_codex), "exec"],
                provider_kind="codex",
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
            )
            context = InvocationContext(
                node_id="node.a",
                task_id="codex_executor_0",
                provider="codex",
                role="executor",
                round_num=1,
                diagnostics=diagnostics.append,
            )

            try:
                await asyncio.wait_for(
                    invoke_agent(
                        config=config,
                        model="gpt-5.4",
                        prompt="review the repository",
                        output_file=output_file,
                        log_file=log_file,
                        invocation_context=context,
                        plan_builder=build_cli_invocation_plan,
                    ),
                    timeout=3.0,
                )
            finally:
                if child_pid_file.exists():
                    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
                    with suppress(ProcessLookupError):
                        os.kill(child_pid, 9)

            self.assertEqual(output_file.read_text(encoding="utf-8"), "final answer")
            self.assertEqual(
                [diagnostic.operation for diagnostic in diagnostics],
                ["process_pipe_drain_timeout"],
            )
            self.assertIn("response.completed", log_file.read_text(encoding="utf-8"))

    async def test_invoke_agent_times_out_hung_provider_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "provider.log"
            pid_file = tmp_path / "provider.pid"
            script_path = tmp_path / "hung_provider.py"
            script_path.write_text(
                "\n".join(
                    [
                        "import os",
                        "import sys",
                        "import time",
                        "from pathlib import Path",
                        "",
                        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')",
                        "print('provider started', flush=True)",
                        "time.sleep(30)",
                    ]
                ),
                encoding="utf-8",
            )
            diagnostics = []
            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path), str(pid_file)],
                default_model=None,
                model_arg=None,
                invocation_timeout_seconds=0.2,
            )
            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role="executor",
                round_num=1,
                diagnostics=diagnostics.append,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "wall-clock timeout reached after 0.2s",
            ):
                await invoke_agent(
                    config=config,
                    model=None,
                    prompt="prompt",
                    output_file=output_file,
                    log_file=log_file,
                    invocation_context=context,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertFalse(output_file.exists())
            self.assertEqual(
                [diagnostic.operation for diagnostic in diagnostics],
                ["invocation_timeout"],
            )
            self.assertEqual(diagnostics[0].attributes["timeout_scope"], "wall_clock")
            self.assertIn("provider started", log_file.read_text(encoding="utf-8"))

            pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while True:
                with suppress(ProcessLookupError):
                    os.kill(pid, 0)
                    if time.monotonic() >= deadline:
                        self.fail("timed out process was not reaped")
                    await asyncio.sleep(0.05)
                    continue
                break

    async def test_invoke_agent_times_out_idle_provider_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "provider.log"
            pid_file = tmp_path / "provider.pid"
            script_path = tmp_path / "idle_provider.py"
            script_path.write_text(
                "\n".join(
                    [
                        "import os",
                        "import sys",
                        "import time",
                        "from pathlib import Path",
                        "",
                        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')",
                        "print('provider became quiet', flush=True)",
                        "time.sleep(30)",
                    ]
                ),
                encoding="utf-8",
            )
            diagnostics = []
            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path), str(pid_file)],
                default_model=None,
                model_arg=None,
                invocation_timeout_seconds=5.0,
                invocation_idle_timeout_seconds=0.2,
            )
            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role="executor",
                round_num=1,
                diagnostics=diagnostics.append,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "produced no output for 0.2s",
            ):
                await invoke_agent(
                    config=config,
                    model=None,
                    prompt="prompt",
                    output_file=output_file,
                    log_file=log_file,
                    invocation_context=context,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertFalse(output_file.exists())
            self.assertEqual(
                [diagnostic.operation for diagnostic in diagnostics],
                ["invocation_idle_timeout"],
            )
            self.assertEqual(
                diagnostics[0].message,
                (
                    "Provider invocation produced no stdout or stderr during the "
                    "idle timeout window."
                ),
            )
            self.assertIn(
                "provider became quiet",
                log_file.read_text(encoding="utf-8"),
            )

            pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while True:
                with suppress(ProcessLookupError):
                    os.kill(pid, 0)
                    if time.monotonic() >= deadline:
                        self.fail("idle timed out process was not reaped")
                    await asyncio.sleep(0.05)
                    continue
                break

    async def test_invoke_agent_with_runner_ignores_codex_transcript_quota_text_when_output_extracted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            diagnostics = []
            usages = []
            payload = "\n".join(
                [
                    (
                        '{"type":"item.completed","item":{"aggregated_output":'
                        '"README says usage limit reset after 5h."}}'
                    ),
                    (
                        '{"type":"response.completed","response":'
                        '{"usage":{"input_tokens":12,"output_tokens":3}}}'
                    ),
                ]
            )

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                output_path = Path(cmd[cmd.index("--output-last-message") + 1])
                output_path.write_text("final answer", encoding="utf-8")
                return CommandResult(returncode=0, stdout_text=payload, stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="codex_executor_0",
                provider="codex",
                role="executor",
                round_num=1,
                diagnostics=diagnostics.append,
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
                quota_reached_retry_delay_seconds=0,
            )
            sleep_mock = AsyncMock()
            with patch(
                "orchestrator_cli.runtime.agent.invocation.loop.asyncio.sleep",
                sleep_mock,
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="gpt-5.4",
                    prompt="review the repository",
                    output_file=output_file,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertEqual(output_file.read_text(encoding="utf-8"), "final answer")
            self.assertEqual(sleep_mock.await_count, 0)
            self.assertEqual(diagnostics, [])
            self.assertEqual(usages[0].output_extraction_status, "success")

    async def test_invoke_agent_with_runner_retries_structured_provider_extracted_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            attempts = {"count": 0}

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                attempts["count"] += 1
                output_path = Path(cmd[cmd.index("--output-last-message") + 1])
                output_text = "retry marker" if attempts["count"] == 1 else "final"
                output_path.write_text(output_text, encoding="utf-8")
                return CommandResult(
                    returncode=0,
                    stdout_text='{"type":"response.completed","response":{}}',
                    stderr_text="",
                )

            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_output_contains=["retry marker"],
            )
            sleep_mock = AsyncMock()
            with patch(
                "orchestrator_cli.runtime.agent.invocation.loop.asyncio.sleep",
                sleep_mock,
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="gpt-5.4",
                    prompt="review the repository",
                    output_file=output_file,
                    log_file=None,
                    invocation_context=None,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertEqual(attempts["count"], 2)
            self.assertEqual(sleep_mock.await_count, 1)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "final")

    def test_parse_codex_usage_prefers_valid_terminal_usage_over_malformed_candidate(
        self,
    ) -> None:
        parsed_usage = parse_provider_usage(
            "codex",
            "\n".join(
                [
                    '{"usage":{"input_tokens":"bad"}}',
                    (
                        '{"type":"response.completed","response":'
                        '{"usage":{"input_tokens":120,"output_tokens":30}}}'
                    ),
                ]
            ),
            "",
        )

        self.assertEqual(parsed_usage.status, "parsed")
        assert parsed_usage.tokens is not None
        self.assertEqual(parsed_usage.tokens.input, 120)
        self.assertEqual(parsed_usage.tokens.output, 30)

    async def test_invoke_agent_with_runner_uses_reported_tokens_for_mixed_costs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []
            payload = '{"type":"response.completed","response":{"usage":{"input_tokens":120}}}'

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                output_path = Path(cmd[cmd.index("--output-last-message") + 1])
                output_path.write_text("done", encoding="utf-8")
                return CommandResult(returncode=0, stdout_text=payload, stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="codex_executor_0",
                provider="codex",
                role="executor",
                round_num=1,
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
                pricing={"input": 1.5, "output": 6.0},
            )
            await invoke_agent_with_runner(
                config=config,
                model="gpt-5.4",
                prompt="review the repository",
                output_file=output_file,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            self.assertEqual(len(usages), 1)
            usage = usages[0]
            expected_cost = ((120 * 1.5) + (1 * 6.0)) / 1_000_000
            self.assertEqual(usage.provider_tokens["input"], 120)
            self.assertIsNone(usage.provider_tokens["output"])
            self.assertEqual(usage.provider_usage_status, "partial")
            self.assertEqual(usage.visible_estimate_tokens, 7)
            self.assertEqual(usage.invocation_cost_confidence, "partial")
            self.assertAlmostEqual(usage.configured_cost_usd or 0.0, expected_cost)

    async def test_invoke_agent_with_runner_fails_when_codex_last_message_is_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []
            payload = '{"type":"response.completed","response":{"usage":{"input_tokens":120,"output_tokens":30}}}'

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                return CommandResult(returncode=0, stdout_text=payload, stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="codex_executor_0",
                provider="codex",
                role="executor",
                round_num=1,
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
                pricing={"input": 1.5, "output": 6.0},
            )
            with self.assertRaisesRegex(
                RuntimeError, "codex output extraction failed: missing"
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="gpt-5.4",
                    prompt="review the repository",
                    output_file=output_file,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertEqual(usages[0].output_extraction_status, "missing")
