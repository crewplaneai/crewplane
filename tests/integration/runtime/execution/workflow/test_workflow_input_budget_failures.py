import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crewplane.adapters.invokers.cli_invoker import (
    build_cli_invocation_plan,
    build_cli_log_presentation,
)
from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    InvocationContext,
    JsonObject,
    LogPresentationDescriptor,
)
from crewplane.artifacts import OutputManager
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.events import ExecutionEvent
from crewplane.runtime.agent.invoker import PlannedAgentInvoker
from crewplane.runtime.execution import WorkflowExecutionError
from crewplane.runtime.workspace.setup import WorkspaceSetupError
from crewplane.version import SCHEMA_VERSION
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
                version=SCHEMA_VERSION,
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
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="source"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
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
                        providers=[
                            ProviderSpec(provider="beta", role=ProviderRole.EXECUTOR)
                        ],
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
                version=SCHEMA_VERSION,
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
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="source"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
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
                        providers=[
                            ProviderSpec(provider="beta", role=ProviderRole.EXECUTOR)
                        ],
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
                and event.payload.operation == "prompt_budget_warning"
                and event.context.node_id == "node.summary"
            ]
            self.assertEqual(warning_events, [])
            self.assertEqual(len(invoker.calls), 2)

    async def test_input_node_materializes_raw_file_and_feeds_downstream_prompt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_file = tmp_path / ".crewplane" / "inputs" / "review-findings.md"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_text("Raw findings from file", encoding="utf-8")
            config = Config(
                version=SCHEMA_VERSION,
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
                        source="{{file:.crewplane/inputs/review-findings.md}}",
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
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
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
            input_file = tmp_path / ".crewplane" / "inputs" / "empty.md"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_text("", encoding="utf-8")

            config = Config(
                version=SCHEMA_VERSION,
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
                        source="{{file:.crewplane/inputs/empty.md}}",
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
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                ],
            )
            invoker = MockAgentInvoker(outputs=["unused"])
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with self.assertRaisesRegex(
                WorkflowExecutionError,
                "Resolved input content for node 'empty-input' is empty after preflight assembly.",
            ):
                await execute_workflow(config, workflow, output, invoker=invoker)

            self.assertEqual(invoker.calls, [])

    async def test_empty_resolved_executor_prompt_is_workflow_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.empty.prompt",
                nodes=[
                    WorkflowNode(
                        id="empty-prompt",
                        mode="parallel",
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED,
                                content="{{env:EMPTY_EXECUTOR_PROMPT}}",
                            )
                        ],
                        providers=[ProviderSpec(provider="alpha")],
                    ),
                    WorkflowNode(
                        id="blocked",
                        mode="sequential",
                        needs=["empty-prompt"],
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED,
                                content="Use {{empty-prompt.output}}",
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                ],
            )
            invoker = MockAgentInvoker(outputs=["unused"])
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with (
                patch.dict("os.environ", {"EMPTY_EXECUTOR_PROMPT": ""}, clear=False),
                self.assertRaises(WorkflowExecutionError) as raised,
            ):
                await execute_workflow(config, workflow, output, invoker=invoker)

            failure_message = str(raised.exception)
            self.assertIn("Workflow 'dag.empty.prompt' failed:", failure_message)
            self.assertIn("- failed: empty-prompt", failure_message)
            self.assertIn(
                "Resolved executor prompt for node 'empty-prompt' is empty",
                failure_message,
            )
            self.assertIn(
                "- blocked: blocked (unsatisfied dependencies: empty-prompt)",
                failure_message,
            )
            self.assertEqual(invoker.calls, [])

    async def test_invalid_findings_output_is_workflow_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.invalid.findings",
                nodes=[
                    WorkflowNode(
                        id="findings-node",
                        mode="parallel",
                        findings=True,
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED,
                                content="produce findings",
                            )
                        ],
                        providers=[ProviderSpec(provider="alpha")],
                    ),
                    WorkflowNode(
                        id="blocked",
                        mode="parallel",
                        needs=["findings-node"],
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED,
                                content="Use {{findings-node.output}}",
                            )
                        ],
                        providers=[ProviderSpec(provider="alpha")],
                    ),
                ],
            )
            invoker = MockAgentInvoker(outputs=["missing findings block"])
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with self.assertRaises(WorkflowExecutionError) as raised:
                await execute_workflow(config, workflow, output, invoker=invoker)

            failure_message = str(raised.exception)
            self.assertIn("Workflow 'dag.invalid.findings' failed:", failure_message)
            self.assertIn("- failed: findings-node", failure_message)
            self.assertIn("Expected exactly one findings block", failure_message)
            self.assertIn(
                "- blocked: blocked (unsatisfied dependencies: findings-node)",
                failure_message,
            )

    async def test_failed_node_blocks_dependents_but_independent_nodes_continue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
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
                        mode="parallel",
                        prompt_segments=[
                            PromptSegment(role=PromptSegmentRole.SHARED, content="fail")
                        ],
                        providers=[ProviderSpec(provider="fail")],
                        failure_threshold=0,
                    ),
                    WorkflowNode(
                        id="node.root.ok",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(role=PromptSegmentRole.SHARED, content="ok")
                        ],
                        providers=[
                            ProviderSpec(provider="ok", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                    WorkflowNode(
                        id="node.dep",
                        mode="sequential",
                        needs=["node.root.fail"],
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="dependent"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="ok", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                ],
            )
            invoker = SelectiveFailInvoker(failing_models={"fail"})
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with self.assertRaises(WorkflowExecutionError) as raised:
                await execute_workflow(config, workflow, output, invoker=invoker)

            failure_message = str(raised.exception)
            self.assertIn("Workflow 'dag.failure' failed:", failure_message)
            self.assertIn("- failed: node.root.fail", failure_message)
            self.assertIn("exceeded failure threshold", failure_message)
            self.assertIn(
                "- blocked: node.dep (unsatisfied dependencies: node.root.fail)",
                failure_message,
            )
            executed_models = sorted(call["model"] for call in invoker.calls)
            self.assertIn("ok", executed_models)
            self.assertIn("fail", executed_models)

    async def test_unexpected_node_task_exception_propagates_without_aggregation(
        self,
    ) -> None:
        class DefectiveOutputManager(OutputManager):
            def finalize_stage(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                raise RuntimeError("simulated finalize defect")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            input_file = tmp_path / ".crewplane" / "inputs" / "source.md"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_text("source", encoding="utf-8")
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.unexpected.node.defect",
                nodes=[
                    WorkflowNode(
                        id="input",
                        mode="input",
                        source="{{file:.crewplane/inputs/source.md}}",
                    )
                ],
            )
            invoker = MockAgentInvoker()
            output = DefectiveOutputManager(workflow.name, base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            with self.assertRaisesRegex(
                RuntimeError,
                "simulated finalize defect",
            ) as raised:
                await execute_workflow(
                    config,
                    workflow,
                    output,
                    invoker=invoker,
                    event_sink=events.append,
                )

            self.assertIs(type(raised.exception), RuntimeError)
            node_failed_events = [
                event
                for event in events
                if event.event_type == "node_failed"
                and event.context.node_id == "input"
            ]
            self.assertEqual(len(node_failed_events), 1)

    async def test_concurrent_unexpected_node_failures_are_drained(
        self,
    ) -> None:
        class DefectiveOutputManager(OutputManager):
            def finalize_stage(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                raise RuntimeError("simulated finalize defect")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            for file_name in ("first.md", "second.md"):
                input_file = tmp_path / ".crewplane" / "inputs" / file_name
                input_file.parent.mkdir(parents=True, exist_ok=True)
                input_file.write_text("source", encoding="utf-8")
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                },
            )
            workflow = WorkflowPlan(
                name="dag.concurrent.unexpected.node.defects",
                nodes=[
                    WorkflowNode(
                        id="first",
                        mode="input",
                        source="{{file:.crewplane/inputs/first.md}}",
                    ),
                    WorkflowNode(
                        id="second",
                        mode="input",
                        source="{{file:.crewplane/inputs/second.md}}",
                    ),
                ],
            )
            invoker = MockAgentInvoker()
            output = DefectiveOutputManager(workflow.name, base_dir=tmp_path)
            events: list[ExecutionEvent] = []
            loop = asyncio.get_running_loop()
            original_handler = loop.get_exception_handler()
            loop_exception_contexts: list[dict[str, object]] = []

            def capture_loop_exception(
                loop: asyncio.AbstractEventLoop,
                context: dict[str, object],
            ) -> None:
                del loop
                loop_exception_contexts.append(context)

            loop.set_exception_handler(capture_loop_exception)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated finalize defect",
                ):
                    await execute_workflow(
                        config,
                        workflow,
                        output,
                        invoker=invoker,
                        event_sink=events.append,
                    )
                await asyncio.sleep(0)
            finally:
                loop.set_exception_handler(original_handler)

            failed_node_ids = [
                event.context.node_id
                for event in events
                if event.event_type == "node_failed"
            ]
            self.assertEqual(failed_node_ids, ["first", "second"])
            never_retrieved_contexts = [
                context
                for context in loop_exception_contexts
                if "exception was never retrieved" in str(context.get("message", ""))
            ]
            self.assertEqual(never_retrieved_contexts, [])

    async def test_unexpected_parallel_invoker_exception_propagates_without_aggregation(
        self,
    ) -> None:
        class DefectiveInvoker:
            def log_presentation_for(
                self,
                config: AgentConfig,  # noqa: ARG002 - Required by invoker protocol.
            ) -> LogPresentationDescriptor | None:
                return None

            async def invoke(
                self,
                config: AgentConfig,  # noqa: ARG002 - Required by invoker protocol.
                model: str | None,
                prompt: str,  # noqa: ARG002 - Required by invoker protocol.
                output_file: Path,
                cwd: Path,  # noqa: ARG002 - Required by invoker protocol.
                log_file: Path | None = None,  # noqa: ARG002 - Required by invoker protocol.
                invocation_context: InvocationContext | None = None,  # noqa: ARG002 - Required by invoker protocol.
            ) -> None:
                if model == "defective":
                    raise TypeError("simulated invoker defect")
                output_file.write_text("success", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "ok": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                    "defective": AgentConfig(
                        cli_cmd=["mock"], default_model="defective"
                    ),
                },
            )
            workflow = WorkflowPlan(
                name="dag.unexpected.invoker.defect",
                nodes=[
                    WorkflowNode(
                        id="parallel",
                        mode="parallel",
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED,
                                content="run defective provider",
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="ok"),
                            ProviderSpec(provider="defective"),
                        ],
                        failure_threshold=1,
                    )
                ],
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with self.assertRaisesRegex(
                TypeError, "simulated invoker defect"
            ) as raised:
                await execute_workflow(
                    config,
                    workflow,
                    output,
                    invoker=DefectiveInvoker(),
                )

            self.assertIs(type(raised.exception), TypeError)

    async def test_workspace_setup_failure_is_aggregated_as_workflow_failure(
        self,
    ) -> None:
        class SetupFailureInvoker:
            def log_presentation_for(
                self,
                config: AgentConfig,  # noqa: ARG002 - Required by invoker protocol.
            ) -> LogPresentationDescriptor | None:
                return None

            async def invoke(
                self,
                config: AgentConfig,  # noqa: ARG002 - Required by invoker protocol.
                model: str | None,  # noqa: ARG002 - Required by invoker protocol.
                prompt: str,  # noqa: ARG002 - Required by invoker protocol.
                output_file: Path,  # noqa: ARG002 - Required by invoker protocol.
                cwd: Path,  # noqa: ARG002 - Required by invoker protocol.
                log_file: Path | None = None,  # noqa: ARG002 - Required by invoker protocol.
                invocation_context: InvocationContext | None = None,  # noqa: ARG002 - Required by invoker protocol.
            ) -> None:
                summary: JsonObject = {"status": "failed"}
                raise WorkspaceSetupError("workspace setup command failed", summary)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "setup": AgentConfig(cli_cmd=["mock"], default_model="setup"),
                    "dependent": AgentConfig(
                        cli_cmd=["mock"], default_model="dependent"
                    ),
                },
            )
            workflow = WorkflowPlan(
                name="dag.workspace.setup.failure",
                nodes=[
                    WorkflowNode(
                        id="node.setup",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED,
                                content="run setup",
                            )
                        ],
                        providers=[ProviderSpec(provider="setup")],
                    ),
                    WorkflowNode(
                        id="node.dep",
                        mode="parallel",
                        needs=["node.setup"],
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED,
                                content="blocked by setup",
                            )
                        ],
                        providers=[ProviderSpec(provider="dependent")],
                    ),
                ],
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)

            with self.assertRaises(WorkflowExecutionError) as raised:
                await execute_workflow(
                    config,
                    workflow,
                    output,
                    invoker=SetupFailureInvoker(),
                )

            failure_message = str(raised.exception)
            self.assertIn("- failed: node.setup", failure_message)
            self.assertIn("workspace setup command failed", failure_message)
            self.assertIn(
                "- blocked: node.dep (unsatisfied dependencies: node.setup)",
                failure_message,
            )

    async def test_blocked_nodes_emit_node_blocked_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
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
                        prompt_segments=[
                            PromptSegment(role=PromptSegmentRole.SHARED, content="fail")
                        ],
                        providers=[
                            ProviderSpec(provider="fail", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                    WorkflowNode(
                        id="node.dep",
                        mode="sequential",
                        needs=["node.root.fail"],
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="dependent"
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="ok", role=ProviderRole.EXECUTOR)
                        ],
                    ),
                ],
            )
            invoker = SelectiveFailInvoker(failing_models={"fail"})
            output = OutputManager(workflow.name, base_dir=tmp_path)
            events = []

            with self.assertRaisesRegex(WorkflowExecutionError, "blocked: node.dep"):
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
            self.assertEqual(blocked_events[0].context.node_id, "node.dep")
            blocked_runtime_logs = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "blocked_dependencies"
            ]
            self.assertEqual(len(blocked_runtime_logs), 1)
            self.assertEqual(blocked_runtime_logs[0].context.node_id, "node.dep")
            self.assertIn(
                "unsatisfied dependencies: node.root.fail",
                blocked_runtime_logs[0].payload.message,
            )

    async def test_nonzero_exit_still_emits_invocation_failed_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(
                        cli_cmd=["./alpha-cli"],
                        default_model="alpha",
                    ),
                },
            )
            workflow = WorkflowPlan(
                name="nonzero.exit",
                nodes=[
                    WorkflowNode(
                        id="node.fail",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(role=PromptSegmentRole.SHARED, content="fail")
                        ],
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
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
                return CommandResult(returncode=2, stdout_text="", stderr_text="boom")

            with (
                patch(
                    "crewplane.runtime.agent.invocation.command.run_command_once",
                    failing_command_runner,
                ),
                self.assertRaisesRegex(RuntimeError, "Exit code 2: boom"),
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

            invocation_failed_events = [
                event for event in events if event.event_type == "invocation_failed"
            ]
            self.assertEqual(len(invocation_failed_events), 1)
            self.assertEqual(invocation_failed_events[0].context.node_id, "node.fail")
            self.assertEqual(
                invocation_failed_events[0].payload.error, "Exit code 2: boom"
            )
            self.assertIsNotNone(invocation_failed_events[0].context.log_file)
