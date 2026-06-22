import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator_cli.adapters.invokers.cli_invoker import (
    build_cli_invocation_plan,
    build_cli_log_presentation,
)
from orchestrator_cli.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
)
from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.core.config import AgentConfig, Config
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.observability.events import ExecutionEvent
from orchestrator_cli.runtime.agent.invoker import PlannedAgentInvoker
from orchestrator_cli.version import SCHEMA_VERSION
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    CleanupOnCancelInvoker,
    execute_workflow,
)


class WorkflowProviderFailureEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_context_failure_emits_classification_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "codex": AgentConfig(
                        cli_cmd=["codex", "exec"],
                        provider_kind="codex",
                        default_model="gpt-test",
                        prompt_transport="stdin",
                        prompt_transport_arg="-",
                    ),
                },
            )
            workflow = WorkflowPlan(
                name="codex.context.failure",
                nodes=[
                    WorkflowNode(
                        id="node.fail",
                        mode="sequential",
                        prompt_segments=[PromptSegment(role="shared", content="fail")],
                        providers=[ProviderSpec(provider="codex", role="executor")],
                    )
                ],
            )
            output = OutputManager(
                workflow.name, base_dir=tmp_path, log_cli_output=True
            )
            events: list[ExecutionEvent] = []

            async def failing_command_runner(
                cmd: list[str],  # noqa: ARG001 - Required by test double or callback signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by test double or callback signature.
                log_file: Path | None,
                append_log: bool,  # noqa: ARG001 - Required by test double or callback signature.
                log_header: bytes | None,
                cwd: Path,  # noqa: ARG001 - Required by test double or callback signature.
                invocation_context,  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by test double or callback signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by test double or callback signature.
            ) -> CommandResult:
                assert log_file is not None
                log_file.parent.mkdir(parents=True, exist_ok=True)
                if log_header is not None:
                    log_file.write_bytes(log_header)
                return CommandResult(
                    returncode=1,
                    stdout_text=(
                        '{"type":"error","message":"Reconnecting... 2/5 '
                        "(stream disconnected before completion: Incomplete response "
                        'returned, reason: max_output_tokens)"}\n'
                        '{"type":"turn.failed","error":{"message":"Codex ran out of '
                        "room in the model's context window. Start a new thread or "
                        'clear earlier history before retrying."}}'
                    ),
                    stderr_text=(
                        "ERROR codex_core::mcp_tool_call: failed to parse tool call "
                        "arguments: EOF while parsing an object"
                    ),
                )

            with (
                patch(
                    "orchestrator_cli.runtime.agent.invocation.command.run_command_once",
                    failing_command_runner,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "Codex ran out of room in the model's context window",
                ),
            ):
                await execute_workflow(
                    config,
                    workflow,
                    output,
                    invoker=PlannedAgentInvoker(
                        plan_builder=build_cli_invocation_plan,
                        log_presentation_builder=build_cli_log_presentation,
                    ),
                    event_sink=events.append,
                )

            failed_events = [
                event for event in events if event.event_type == "invocation_failed"
            ]
            self.assertEqual(len(failed_events), 1)
            failed_event = failed_events[0]
            self.assertEqual(
                failed_event.payload.failure_kind,
                "provider_session_context_exhausted",
            )
            self.assertEqual(failed_event.payload.failure_phase, "provider_session")
            self.assertEqual(failed_event.payload.failure_source, "stdout_json")
            self.assertIn(
                "Split the workflow", failed_event.payload.failure_advice or ""
            )

    async def test_provider_quota_guard_failure_emits_classification_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "codex": AgentConfig(
                        cli_cmd=["codex", "exec"],
                        provider_kind="codex",
                        default_model="gpt-test",
                        prompt_transport="stdin",
                        prompt_transport_arg="-",
                        quota_reached_on_contains=["usage limit reached"],
                        quota_reached_retry_delay_seconds=0,
                    ),
                },
            )
            workflow = WorkflowPlan(
                name="codex.quota.failure",
                nodes=[
                    WorkflowNode(
                        id="node.fail",
                        mode="sequential",
                        prompt_segments=[PromptSegment(role="shared", content="fail")],
                        providers=[ProviderSpec(provider="codex", role="executor")],
                    )
                ],
            )
            output = OutputManager(
                workflow.name, base_dir=tmp_path, log_cli_output=True
            )
            events: list[ExecutionEvent] = []

            async def quota_command_runner(
                cmd: list[str],  # noqa: ARG001 - Required by test double or callback signature.
                stdin_data: bytes | None,  # noqa: ARG001 - Required by test double or callback signature.
                log_file: Path | None,  # noqa: ARG001 - Required by test double or callback signature.
                append_log: bool,  # noqa: ARG001 - Required by test double or callback signature.
                log_header: bytes | None,  # noqa: ARG001 - Required by test double or callback signature.
                cwd: Path,  # noqa: ARG001 - Required by test double or callback signature.
                invocation_context,  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by test double or callback signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001 - Required by test double or callback signature.
            ) -> CommandResult:
                return CommandResult(
                    returncode=0,
                    stdout_text="usage limit reached",
                    stderr_text="",
                )

            with (
                patch(
                    "orchestrator_cli.runtime.agent.invocation.command.run_command_once",
                    quota_command_runner,
                ),
                patch(
                    "orchestrator_cli.runtime.agent.invocation.retry.quota_retry_guard_exhausted",
                    side_effect=[False, True],
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "Quota retry guard exceeded after 5 hours",
                ),
            ):
                await execute_workflow(
                    config,
                    workflow,
                    output,
                    invoker=PlannedAgentInvoker(
                        plan_builder=build_cli_invocation_plan,
                        log_presentation_builder=build_cli_log_presentation,
                    ),
                    event_sink=events.append,
                )

            failed_events = [
                event for event in events if event.event_type == "invocation_failed"
            ]
            self.assertEqual(len(failed_events), 1)
            failed_event = failed_events[0]
            self.assertEqual(failed_event.payload.failure_kind, "quota_or_rate_limit")
            self.assertEqual(failed_event.payload.failure_phase, "provider_transport")
            self.assertIn("quota", failed_event.payload.failure_advice or "")

    async def test_execute_workflow_awaits_cancelled_node_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                },
            )
            workflow = WorkflowPlan(
                name="cancel.cleanup",
                nodes=[
                    WorkflowNode(
                        id="node.long",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(role="shared", content="run long")
                        ],
                        providers=[ProviderSpec(provider="alpha", role="executor")],
                    )
                ],
            )
            invoker = CleanupOnCancelInvoker(cleanup_delay_seconds=0.05)
            output = OutputManager(workflow.name, base_dir=tmp_path)

            async def _fail_wait_for_completed_nodes(state):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                await invoker.started.wait()
                raise RuntimeError("simulated scheduler failure")

            with (
                patch(
                    "orchestrator_cli.runtime.execution.workflow.wait_for_completed_nodes",
                    new=_fail_wait_for_completed_nodes,
                ),
                self.assertRaisesRegex(RuntimeError, "simulated scheduler failure"),
            ):
                await execute_workflow(config, workflow, output, invoker=invoker)

            self.assertTrue(invoker.cleanup_started.is_set())
            self.assertTrue(invoker.cleanup_finished.is_set())
