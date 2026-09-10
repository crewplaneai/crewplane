import tempfile
import unittest
from pathlib import Path

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
from crewplane.runtime.agent.usage import InvocationUsage, estimate_token_count


class InvocationLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_records_usage_once(self) -> None:
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
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role=ProviderRole.EXECUTOR,
                usage_recorder=usages.append,
            )
            await invoke_agent_with_runner(
                config=AgentConfig(cli_cmd=["echo"], default_model="test"),
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

    async def test_visible_output_streams_persisted_stdout_to_final_artifact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            stream_path = tmp_path / "captured-stdout.txt"
            output_text = "line 1\n" + ("x" * 10_000)
            stream_path.write_text(output_text, encoding="utf-8")
            usages: list[InvocationUsage] = []

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
                return CommandResult(
                    returncode=0,
                    stdout_text="x" * 32,
                    stderr_text="",
                    stdout_path=stream_path,
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role=ProviderRole.EXECUTOR,
                usage_recorder=usages.append,
            )
            await invoke_agent_with_runner(
                config=AgentConfig(cli_cmd=["echo"], default_model="test"),
                model="test",
                prompt="prompt",
                output_file=output_file,
                cwd=tmp_path,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            assert output_file.read_text(encoding="utf-8") == output_text
            assert not stream_path.exists()
            assert len(usages) == 1
            assert usages[0].visible_estimate_tokens == estimate_token_count(
                len("prompt")
            ) + estimate_token_count(len(output_text))

    async def test_structured_provider_retry_records_usage_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            usages: list[InvocationUsage] = []

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
                output_path = Path(cmd[cmd.index("--output-last-message") + 1])
                output_text = "retry" if not append_log else "f"
                output_path.write_text(output_text, encoding="utf-8")
                input_tokens, output_tokens = (2, 3) if not append_log else (4, 5)
                return CommandResult(
                    returncode=0,
                    stdout_text=(
                        '{"type":"turn.completed","usage":{'
                        '"input_tokens":'
                        f"{input_tokens},"
                        f'"output_tokens":{output_tokens}'
                        "}}"
                    ),
                    stderr_text="",
                )

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
                retry_on_output_contains=["retry"],
            )

            await invoke_agent_with_runner(
                config=config,
                model="gpt-5.5",
                prompt="",
                output_file=output_file,
                cwd=Path(tmp_dir),
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            assert len(usages) == 1
            usage = usages[0]
            assert usage.visible_estimate_tokens == 2
            assert usage.provider_usage_report_count == 2
            assert usage.provider_tokens["input"] == 6
            assert usage.provider_tokens["output"] == 8
            assert usage.provider_tokens["total"] == 14
            assert output_file.read_text(encoding="utf-8") == "f"

    async def test_usage_decoder_reads_stream_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usage_records: list[InvocationUsage] = []
            stream_path = tmp_path / "stdout.capture"

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
                stream_path.write_text(
                    '{"type":"turn.completed","usage":{"input_tokens":7,'
                    '"output_tokens":3}}',
                    encoding="utf-8",
                )
                structured_path = Path(cmd[cmd.index("--output-last-message") + 1])
                structured_path.write_text("done", encoding="utf-8")
                return CommandResult(
                    returncode=0,
                    stdout_text="",
                    stderr_text="",
                    stdout_path=stream_path,
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="codex_executor_0",
                provider="codex",
                role=ProviderRole.EXECUTOR,
                usage_recorder=usage_records.append,
            )
            await invoke_agent_with_runner(
                config=AgentConfig(
                    cli_cmd=["codex", "exec"],
                    provider_kind="codex",
                    default_model="gpt-5.5",
                    prompt_transport="stdin",
                    prompt_transport_arg="-",
                ),
                model="gpt-5.5",
                prompt="prompt",
                output_file=output_file,
                cwd=tmp_path,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            assert len(usage_records) == 1
            assert usage_records[0].provider_usage_report_count == 1
            assert usage_records[0].provider_tokens["input"] == 7
            assert not stream_path.exists()

    async def test_gemini_and_kilo_usage_are_recorded_by_injected_capabilities(
        self,
    ) -> None:
        cases = (
            (
                "gemini",
                '{"response":"Gemini","stats":{"models":{"main":{"tokens":{'
                '"prompt":10,"cached":2,"candidates":3,"thoughts":1,"tool":0,'
                '"total":14}}}}}',
                "Gemini",
                {
                    "input": 10,
                    "cached_input": 2,
                    "output": 4,
                    "reasoning": 1,
                    "total": 14,
                },
                1,
            ),
            (
                "kilo",
                '{"type":"text","part":{"text":"Kilo"}}\n'
                '{"type":"step_finish","part":{"tokens":{"input":10,'
                '"output":3,"reasoning":1,"cache":{"read":2,"write":1}}}}\n'
                '{"type":"step_finish","part":{"tokens":{"input":0,'
                '"output":0,"reasoning":0,"cache":{"read":0,"write":0}}}}',
                "Kilo\n",
                {
                    "input": 13,
                    "cached_input": 2,
                    "cache_write": 1,
                    "output": 4,
                    "reasoning": 1,
                    "total": 17,
                },
                2,
            ),
        )
        for (
            provider,
            stdout_text,
            expected_output,
            expected_tokens,
            expected_report_count,
        ) in cases:
            with (
                self.subTest(provider=provider),
                tempfile.TemporaryDirectory() as tmp_dir,
            ):
                tmp_path = Path(tmp_dir)
                output_file = tmp_path / "output.txt"
                usage_records: list[InvocationUsage] = []
                case_stdout_text = stdout_text

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
                    captured_stdout: str = case_stdout_text,
                ) -> CommandResult:
                    return CommandResult(0, captured_stdout, "")

                context = InvocationContext(
                    node_id="node.a",
                    task_id=f"{provider}_executor_0",
                    provider=provider,
                    role=ProviderRole.EXECUTOR,
                    usage_recorder=usage_records.append,
                )
                await invoke_agent_with_runner(
                    config=AgentConfig(
                        cli_cmd=[provider],
                        provider_kind=provider,
                        default_model="test",
                    ),
                    model="test",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=tmp_path,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

                assert output_file.read_text(encoding="utf-8") == expected_output
                assert len(usage_records) == 1
                assert (
                    usage_records[0].provider_usage_report_count
                    == expected_report_count
                )
                for bucket, value in expected_tokens.items():
                    assert usage_records[0].provider_tokens[bucket] == value

    async def test_failure_records_usage_once_before_reraising(self) -> None:
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
                    returncode=1,
                    stdout_text="",
                    stderr_text="fatal",
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role=ProviderRole.EXECUTOR,
                usage_recorder=usages.append,
            )
            with pytest.raises(InvocationFailureError):
                await invoke_agent_with_runner(
                    config=AgentConfig(cli_cmd=["echo"], default_model="test"),
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
