import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from orchestrator_cli.core.config import AgentConfig, Config
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionPlan,
    ProviderRecord,
)
from orchestrator_cli.core.preflight.secrets import SecretContext
from orchestrator_cli.core.preflight.signatures import signature_for_payload
from orchestrator_cli.core.versions import CONFIG_SCHEMA_VERSION
from orchestrator_cli.runtime.agent.invoker import (
    invoke_agent_with_runner,
)
from orchestrator_cli.runtime.agent.quota import (
    classify_quota,
)
from orchestrator_cli.runtime.agent.types import CommandResult, InvocationContext
from orchestrator_cli.runtime.execution.common import (
    CompiledRuntimeContext,
    resolve_provider_model,
)


class InvocationContextAndModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_invoke_agent_with_runner_receives_invocation_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            captured: dict[str, str] = {}

            async def runner(
                cmd: list[str],  # noqa: ARG001 - Required by callback or protocol signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                if invocation_context is not None:
                    captured["task_id"] = invocation_context.task_id
                    captured["node_id"] = invocation_context.node_id
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="alpha_executor_0",
                provider="alpha",
                role="executor",
                round_num=1,
            )
            config = AgentConfig(
                cli_cmd=["echo"],
                default_model="test",
            )
            await invoke_agent_with_runner(
                config=config,
                model="test-model",
                prompt="prompt",
                output_file=output_file,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
            )
            self.assertEqual(captured["node_id"], "node.a")
            self.assertEqual(captured["task_id"], "alpha_executor_0")

    async def test_gemini_auto_model_uses_headless_defaults(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            captured: dict[str, list[str]] = {}

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                captured["cmd"] = cmd
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            config = AgentConfig(
                cli_cmd=["gemini"],
                default_model="auto",
                model_arg=None,
                prompt_arg=None,
                use_stdin=False,
                extra_args=["--approval-mode=yolo"],
            )
            await invoke_agent_with_runner(
                config=config,
                model="auto",
                prompt="review the repository",
                output_file=output_file,
                log_file=None,
                invocation_context=None,
                command_runner=runner,
            )

            self.assertEqual(
                captured["cmd"],
                [
                    "gemini",
                    "--model",
                    "auto",
                    "--approval-mode=yolo",
                    "--prompt",
                    "review the repository",
                ],
            )
            self.assertEqual(output_file.read_text(encoding="utf-8"), "ok")

    async def test_gemini_omits_model_flag_without_resolved_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            captured: dict[str, list[str]] = {}

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                captured["cmd"] = cmd
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            config = AgentConfig(
                cli_cmd=["gemini"],
                model_arg=None,
                prompt_arg=None,
                use_stdin=False,
                extra_args=["--approval-mode=yolo"],
            )
            await invoke_agent_with_runner(
                config=config,
                model=None,
                prompt="review the repository",
                output_file=output_file,
                log_file=None,
                invocation_context=None,
                command_runner=runner,
            )

            self.assertEqual(
                captured["cmd"],
                [
                    "gemini",
                    "--approval-mode=yolo",
                    "--prompt",
                    "review the repository",
                ],
            )
            self.assertEqual(output_file.read_text(encoding="utf-8"), "ok")

    def test_resolve_provider_model_prefers_workflow_override_over_default_model(
        self,
    ) -> None:
        config = Config(
            version=CONFIG_SCHEMA_VERSION,
            agents={
                "alpha": AgentConfig(
                    cli_cmd=["echo"],
                    default_model="config-model",
                )
            },
        )
        agent_payload = config.agents["alpha"].model_dump(
            mode="json",
            exclude_none=True,
        )
        invoker_payload = {
            "api_version": "test",
            "capabilities": {},
            "implementation": "mock",
            "options": {},
            "resolved_identity": "mock",
        }

        runtime_context = CompiledRuntimeContext(
            plan=PreflightExecutionPlan(
                run_id="run",
                run_key_name="workflow-run",
                context_root="/tmp/workflow-run",
                manifest_root="/tmp/workflow-run/manifests",
                created_at=datetime(2026, 6, 3).isoformat(),
                workflow_name="workflow",
                workflow_signature="0" * 64,
                execution_order=[],
                nodes=[],
                render_plans=[],
                static_resources=[],
                token_catalog=[],
                dependency_graph=[],
                runtime_config_snapshot={
                    "agents": {"alpha": agent_payload},
                    "execution": {},
                    "invoker": {**invoker_payload, "option_scopes": {}},
                },
                effective_runtime_config_signature="1" * 64,
            ),
            secret_context=SecretContext(),
        )

        _, resolved_model = resolve_provider_model(
            runtime_context,
            ProviderRecord(
                provider="alpha",
                model="workflow-model",
                role="executor",
                task_id="alpha_executor_0",
                agent_config_key="alpha",
                invoker_alias="mock",
                agent_config_signature=signature_for_payload(
                    {
                        "agent_config": agent_payload,
                        "agent_config_key": "alpha",
                    }
                ),
                invoker_config_signature=signature_for_payload(invoker_payload),
            ),
        )

        self.assertEqual(resolved_model, "workflow-model")

    async def test_copilot_standalone_cli_uses_programmatic_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            captured: dict[str, list[str]] = {}

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                log_file: Path | None,  # noqa: ARG001 - Required by callback or protocol signature.
                append_log: bool,  # noqa: ARG001 - Required by callback or protocol signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by callback or protocol signature.
                invocation_context: InvocationContext | None,  # noqa: ARG001 - Required by callback or protocol signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
            ) -> CommandResult:
                captured["cmd"] = cmd
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            config = AgentConfig(
                cli_cmd=["copilot"],
                default_model="claude-sonnet-4.5",
                extra_args=[
                    "--silent",
                    "--no-ask-user",
                    "--allow-tool=write,shell(git:*)",
                ],
            )
            await invoke_agent_with_runner(
                config=config,
                model="claude-sonnet-4.5",
                prompt="review the repository",
                output_file=output_file,
                log_file=None,
                invocation_context=None,
                command_runner=runner,
            )

            self.assertEqual(
                captured["cmd"],
                [
                    "copilot",
                    "--model",
                    "claude-sonnet-4.5",
                    "--silent",
                    "--no-ask-user",
                    "--allow-tool=write,shell(git:*)",
                    "--prompt",
                    "review the repository",
                ],
            )
            self.assertEqual(output_file.read_text(encoding="utf-8"), "ok")

    def test_copilot_auto_quota_parser_supports_standalone_cli(self) -> None:
        config = AgentConfig(
            cli_cmd=["copilot"],
            default_model="claude-sonnet-4.5",
        )

        quota = classify_quota(
            config=config,
            result=CommandResult(
                returncode=0,
                stdout_text="rate limit reached, retry after 3s",
                stderr_text="",
            ),
            cli_executable="copilot",
        )

        self.assertTrue(quota.is_quota)
        self.assertEqual(quota.evidence, "rate limit")
        self.assertAlmostEqual(quota.reset_after_seconds or 0.0, 3.0, delta=0.2)

    def test_copilot_quota_classifier_ignores_bare_quota_in_success_report(
        self,
    ) -> None:
        config = AgentConfig(
            cli_cmd=["copilot"],
            default_model="claude-sonnet-4.5",
            quota_reached_on_contains=["rate limit", "quota", "too many requests"],
        )

        quota = classify_quota(
            config=config,
            result=CommandResult(
                returncode=0,
                stdout_text=(
                    "Consolidated report: quota handling and rate limit handling "
                    "are implementation details. Retry after 3s examples and "
                    "HTTP 429 handling are not provider errors. Too many requests "
                    "handling belongs in tests."
                ),
                stderr_text="",
            ),
            cli_executable="copilot",
        )

        self.assertFalse(quota.is_quota)
        self.assertIsNone(quota.evidence)

    def test_copilot_quota_classifier_accepts_error_shaped_quota_variants(
        self,
    ) -> None:
        config = AgentConfig(
            cli_cmd=["copilot"],
            default_model="claude-sonnet-4.5",
            quota_reached_on_contains=["rate limit", "quota", "too many requests"],
        )

        cases = {
            "rate limit, retry after 3s": "rate limit",
            "quota, retry after 3s": "quota reached",
            "Error: 429": "429",
            "Request failed: too many requests. Retry after 3s.": ("too many requests"),
            "You are sending too many requests. Retry after 3s.": ("too many requests"),
        }
        for output_text, expected_evidence in cases.items():
            with self.subTest(output_text=output_text):
                quota = classify_quota(
                    config=config,
                    result=CommandResult(
                        returncode=0,
                        stdout_text=output_text,
                        stderr_text="",
                    ),
                    cli_executable="copilot",
                )

                self.assertTrue(quota.is_quota)
                self.assertEqual(quota.evidence, expected_evidence)
