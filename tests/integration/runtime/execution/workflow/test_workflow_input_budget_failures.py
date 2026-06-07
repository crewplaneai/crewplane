import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.core.config import AgentConfig, Config, Settings
from orchestrator_cli.core.versions import CONFIG_SCHEMA_VERSION
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.observability.events import ExecutionEvent
from orchestrator_cli.runtime.agent.invoker import DefaultAgentInvoker
from orchestrator_cli.runtime.agent.types import CommandResult
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    SelectiveFailInvoker,
    execute_workflow,
)


class WorkflowInputBudgetFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_sequential_node_fails_before_invocation_when_prompt_budget_exceeded(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                settings=Settings(
                    token_budget={
                        "warn_threshold_chars": None,
                        "fail_threshold_chars": 10,
                    }
                ),
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="beta"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.prompt.budget.fail",
                nodes=[
                    WorkflowNode(
                        id="node.source",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(role="shared", content="source")
                        ],
                        providers=[ProviderSpec(provider="alpha", role="executor")],
                    ),
                    WorkflowNode(
                        id="node.summary",
                        mode="sequential",
                        needs=["node.source"],
                        prompt_segments=[
                            PromptSegment(
                                role="shared", content="Use {{node.source.output}}"
                            )
                        ],
                        providers=[ProviderSpec(provider="beta", role="executor")],
                    ),
                ],
            )
            invoker = MockAgentInvoker(outputs=["01234567890123456789", "unused"])
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with self.assertRaisesRegex(RuntimeError, "Prompt budget exceeded"):
                await execute_workflow(config, workflow, output, invoker=invoker)

            self.assertEqual(len(invoker.calls), 1)

    async def test_node_token_budget_override_disables_inherited_warn_threshold(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                settings=Settings(token_budget={"warn_threshold_chars": 10}),
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="beta"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.prompt.budget.override",
                nodes=[
                    WorkflowNode(
                        id="node.source",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(role="shared", content="source")
                        ],
                        providers=[ProviderSpec(provider="alpha", role="executor")],
                    ),
                    WorkflowNode(
                        id="node.summary",
                        mode="sequential",
                        needs=["node.source"],
                        prompt_segments=[
                            PromptSegment(
                                role="shared", content="Use {{node.source.output}}"
                            )
                        ],
                        token_budget={"warn_threshold_chars": None},
                        providers=[ProviderSpec(provider="beta", role="executor")],
                    ),
                ],
            )
            invoker = MockAgentInvoker(outputs=["01234567890123456789", "done"])
            output = OutputManager(workflow.name, base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            await execute_workflow(
                config,
                workflow,
                output,
                invoker=invoker,
                event_sink=events.append,
            )

            warning_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.operation == "prompt_budget_warning"
                and event.node_id == "node.summary"
            ]
            self.assertEqual(warning_events, [])
            self.assertEqual(len(invoker.calls), 2)

    async def test_input_node_materializes_raw_file_and_feeds_downstream_prompt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_file = tmp_path / ".orchestrator" / "inputs" / "review-findings.md"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_text("Raw findings from file", encoding="utf-8")
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                },
            )
            workflow = WorkflowPlan(
                name="input.workflow",
                inputs={"review_input": "review-input"},
                nodes=[
                    WorkflowNode(
                        id="review-input",
                        mode="input",
                        source="{{file:.orchestrator/inputs/review-findings.md}}",
                    ),
                    WorkflowNode(
                        id="implement",
                        mode="sequential",
                        needs=["review-input"],
                        prompt_segments=[
                            PromptSegment(
                                role="shared", content="Use {{review-input.output}}"
                            )
                        ],
                        providers=[ProviderSpec(provider="alpha", role="executor")],
                    ),
                ],
            )
            invoker = MockAgentInvoker(outputs=["fixed"])
            output = OutputManager(workflow.name, base_dir=tmp_path)

            await execute_workflow(config, workflow, output, invoker=invoker)

            input_node_dir = output.get_stage_dir("review-input")
            if input_node_dir is None:
                self.fail("Expected input node directory to be created")
            self.assertEqual(
                (input_node_dir / "input_round1.md").read_text(encoding="utf-8"),
                "Raw findings from file",
            )
            self.assertEqual(
                output.get_stage_output_path("review-input").read_text(
                    encoding="utf-8"
                ),
                "Raw findings from file",
            )
            self.assertIn("Raw findings from file", invoker.calls[0]["prompt"])

    async def test_input_node_fails_when_resolved_source_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_file = tmp_path / ".orchestrator" / "inputs" / "empty.md"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_text("", encoding="utf-8")

            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                },
            )
            workflow = WorkflowPlan(
                name="input.empty.workflow",
                nodes=[
                    WorkflowNode(
                        id="empty-input",
                        mode="input",
                        source="{{file:.orchestrator/inputs/empty.md}}",
                    ),
                    WorkflowNode(
                        id="downstream",
                        mode="sequential",
                        needs=["empty-input"],
                        prompt_segments=[
                            PromptSegment(
                                role="shared",
                                content="Use {{empty-input.output}}",
                            )
                        ],
                        providers=[ProviderSpec(provider="alpha", role="executor")],
                    ),
                ],
            )
            invoker = MockAgentInvoker(outputs=["unused"])
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with self.assertRaisesRegex(
                RuntimeError,
                "Resolved input content for node 'empty-input' is empty after preflight assembly.",
            ):
                await execute_workflow(config, workflow, output, invoker=invoker)

            self.assertEqual(invoker.calls, [])

    async def test_failed_node_blocks_dependents_but_independent_nodes_continue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "ok": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                    "fail": AgentConfig(cli_cmd=["mock"], default_model="fail"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.failure",
                nodes=[
                    WorkflowNode(
                        id="node.root.fail",
                        mode="sequential",
                        prompt_segments=[PromptSegment(role="shared", content="fail")],
                        providers=[ProviderSpec(provider="fail", role="executor")],
                    ),
                    WorkflowNode(
                        id="node.root.ok",
                        mode="sequential",
                        prompt_segments=[PromptSegment(role="shared", content="ok")],
                        providers=[ProviderSpec(provider="ok", role="executor")],
                    ),
                    WorkflowNode(
                        id="node.dep",
                        mode="sequential",
                        needs=["node.root.fail"],
                        prompt_segments=[
                            PromptSegment(role="shared", content="dependent")
                        ],
                        providers=[ProviderSpec(provider="ok", role="executor")],
                    ),
                ],
            )
            invoker = SelectiveFailInvoker(failing_models={"fail"})
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with self.assertRaisesRegex(RuntimeError, "blocked: node.dep"):
                await execute_workflow(config, workflow, output, invoker=invoker)

            executed_models = sorted(call["model"] for call in invoker.calls)
            self.assertIn("ok", executed_models)
            self.assertIn("fail", executed_models)

    async def test_blocked_nodes_emit_node_blocked_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "ok": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                    "fail": AgentConfig(cli_cmd=["mock"], default_model="fail"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.blocked.events",
                nodes=[
                    WorkflowNode(
                        id="node.root.fail",
                        mode="sequential",
                        prompt_segments=[PromptSegment(role="shared", content="fail")],
                        providers=[ProviderSpec(provider="fail", role="executor")],
                    ),
                    WorkflowNode(
                        id="node.dep",
                        mode="sequential",
                        needs=["node.root.fail"],
                        prompt_segments=[
                            PromptSegment(role="shared", content="dependent")
                        ],
                        providers=[ProviderSpec(provider="ok", role="executor")],
                    ),
                ],
            )
            invoker = SelectiveFailInvoker(failing_models={"fail"})
            output = OutputManager(workflow.name, base_dir=tmp_path)
            events = []

            with self.assertRaisesRegex(RuntimeError, "blocked: node.dep"):
                await execute_workflow(
                    config,
                    workflow,
                    output,
                    invoker=invoker,
                    event_sink=events.append,
                )

            blocked_events = [
                event for event in events if event.event_type == "node_blocked"
            ]
            self.assertEqual(len(blocked_events), 1)
            self.assertEqual(blocked_events[0].node_id, "node.dep")
            blocked_runtime_logs = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.operation == "blocked_dependencies"
            ]
            self.assertEqual(len(blocked_runtime_logs), 1)
            self.assertEqual(blocked_runtime_logs[0].node_id, "node.dep")
            self.assertIn(
                "unsatisfied dependencies: node.root.fail",
                blocked_runtime_logs[0].message,
            )

    async def test_nonzero_exit_still_emits_invocation_failed_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["alpha-cli"], default_model="alpha"),
                },
            )
            workflow = WorkflowPlan(
                name="nonzero.exit",
                nodes=[
                    WorkflowNode(
                        id="node.fail",
                        mode="sequential",
                        prompt_segments=[PromptSegment(role="shared", content="fail")],
                        providers=[ProviderSpec(provider="alpha", role="executor")],
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
                invocation_context,  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by test double or callback signature.
            ) -> CommandResult:
                assert log_file is not None
                log_file.parent.mkdir(parents=True, exist_ok=True)
                if log_header is not None:
                    log_file.write_bytes(log_header)
                return CommandResult(returncode=2, stdout_text="", stderr_text="boom")

            with (
                patch(
                    "orchestrator_cli.runtime.agent.invocation.command.run_command_once",
                    failing_command_runner,
                ),
                self.assertRaisesRegex(RuntimeError, "Exit code 2: boom"),
            ):
                await execute_workflow(
                    config,
                    workflow,
                    output,
                    invoker=DefaultAgentInvoker(),
                    event_sink=events.append,
                )

            invocation_failed_events = [
                event for event in events if event.event_type == "invocation_failed"
            ]
            self.assertEqual(len(invocation_failed_events), 1)
            self.assertEqual(invocation_failed_events[0].node_id, "node.fail")
            self.assertEqual(invocation_failed_events[0].error, "Exit code 2: boom")
            self.assertIsNotNone(invocation_failed_events[0].log_file)
