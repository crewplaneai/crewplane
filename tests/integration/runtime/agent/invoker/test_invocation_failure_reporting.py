import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.adapters.invokers.cli_invoker import build_cli_invocation_plan
from orchestrator_cli.architecture.contracts import CommandResult, InvocationContext
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.failures import (
    InvocationFailureError,
    classify_invocation_failure,
)
from orchestrator_cli.runtime.agent.failures.patterns import MAX_FAILURE_LINES
from orchestrator_cli.runtime.agent.invoker import (
    invoke_agent_with_runner,
)


class InvocationFailureReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_invoke_agent_with_runner_summarizes_multiline_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "agent.log"

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                assert log_file is not None
                return CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text=(
                        "YOLO mode is enabled. All tool calls will be automatically approved.\n"
                        "Loaded cached credentials.\n"
                        "Fatal error: permission denied\n"
                        "    at runner.js:12:5"
                    ),
                )

            config = AgentConfig(
                cli_cmd=["echo"],
                default_model="test-model",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                rf"Exit code 1: Fatal error: permission denied \(see {log_file}\)",
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="test-model",
                    prompt="prompt",
                    output_file=output_file,
                    log_file=log_file,
                    invocation_context=None,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

    async def test_invoke_agent_with_runner_prefers_structured_failure_message(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "agent.log"

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                return CommandResult(
                    returncode=1,
                    stdout_text=(
                        '{"type":"error","message":"Reconnecting..."}\n'
                        '{"type":"turn.failed","error":{"message":"Codex ran out of room in the model context window."}}'
                    ),
                    stderr_text=(
                        "ERROR codex_core::mcp_tool_call: failed to parse tool call "
                        "arguments: EOF while parsing an object"
                    ),
                )

            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="test-model",
                prompt_transport="stdin",
                prompt_transport_arg="-",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                rf"Exit code 1: Codex ran out of room in the model context window\. \(see {log_file}\)",
            ) as caught:
                await invoke_agent_with_runner(
                    config=config,
                    model="test-model",
                    prompt="prompt",
                    output_file=output_file,
                    log_file=log_file,
                    invocation_context=None,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )
            self.assertIsInstance(caught.exception, InvocationFailureError)
            failure = caught.exception
            assert isinstance(failure, InvocationFailureError)
            self.assertEqual(failure.kind, "provider_session_context_exhausted")
            self.assertEqual(failure.phase, "provider_session")
            self.assertEqual(failure.source, "stdout_json")

    def test_classifies_provider_failure_categories(self) -> None:
        cases = [
            (
                "claude",
                CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text="Error: Prompt is too long for this model.",
                ),
                "initial_request_too_large",
                "initial_request",
                "stderr_text",
            ),
            (
                "copilot",
                CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text="The context window is full. Start a new session.",
                ),
                "provider_session_context_exhausted",
                "provider_session",
                "stderr_text",
            ),
            (
                "codex",
                CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text="The initial request exceeds the model context window.",
                ),
                "initial_request_too_large",
                "initial_request",
                "stderr_text",
            ),
            (
                "codex",
                CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text=(
                        "Maximum context length exceeded in the context window "
                        "after provider tool calls."
                    ),
                ),
                "provider_session_context_exhausted",
                "provider_session",
                "stderr_text",
            ),
            (
                "gemini",
                CommandResult(
                    returncode=53,
                    stdout_text="",
                    stderr_text=(
                        '{"error":{"message":"Turn limit exceeded because the '
                        'context window is full."}}'
                    ),
                ),
                "provider_session_context_exhausted",
                "provider_session",
                "stderr_json",
            ),
            (
                "gemini",
                CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text='{"error":{"code":429,"status":"RESOURCE_EXHAUSTED"}}',
                ),
                "quota_or_rate_limit",
                "provider_transport",
                "stderr_json",
            ),
            (
                "kilo",
                CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text="Automatic compaction failed: conversation too long.",
                ),
                "provider_session_context_exhausted",
                "provider_session",
                "stderr_text",
            ),
            (
                "generic",
                CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text="Generation stopped at max_output_tokens.",
                ),
                "provider_output_limit_exceeded",
                "provider_output",
                "stderr_text",
            ),
        ]
        for (
            provider_kind,
            result,
            expected_kind,
            expected_phase,
            expected_source,
        ) in cases:
            with self.subTest(provider_kind=provider_kind):
                summary = classify_invocation_failure(provider_kind, result)

                self.assertEqual(summary.kind, expected_kind)
                self.assertEqual(summary.phase, expected_phase)
                self.assertEqual(summary.source, expected_source)

    def test_classifies_provider_failure_from_persisted_stderr_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "provider.log"
            path.write_text(
                "\n".join(
                    ["noise"] * 500
                    + ["Automatic compaction failed: conversation too long."]
                ),
                encoding="utf-8",
            )
            summary = classify_invocation_failure(
                "kilo",
                CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text="",
                    stderr_path=path,
                ),
            )

            self.assertEqual(summary.kind, "provider_session_context_exhausted")
            self.assertEqual(summary.phase, "provider_session")
            self.assertEqual(summary.source, "stderr_text")

    def test_classifies_provider_failure_from_marker_beyond_retained_summary_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "provider.log"
            marker = "Automatic compaction failed: conversation too long."
            path.write_text(
                "\n".join(["noise"] * (MAX_FAILURE_LINES + 5) + [marker]),
                encoding="utf-8",
            )
            summary = classify_invocation_failure(
                "kilo",
                CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text="",
                    stderr_path=path,
                ),
            )

            self.assertEqual(summary.kind, "provider_session_context_exhausted")
            self.assertEqual(summary.phase, "provider_session")
            self.assertEqual(summary.source, "stderr_text")
