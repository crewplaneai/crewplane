import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.architecture.contracts import (
    EventType,
    build_result_filename,
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
from crewplane.runtime.execution import WorkflowExecutionError
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
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

            with pytest.raises(RuntimeError, match="Prompt budget exceeded"):
                await execute_workflow(config, workflow, output, invoker=invoker)

            assert len(invoker.calls) == 1

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
                if event.event_type == EventType.RUNTIME_LOG
                and event.payload.operation == "prompt_budget_warning"
                and event.context.node_id == "node.summary"
            ]
            assert warning_events == []
            assert len(invoker.calls) == 2

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

            input_node_dir = output.get_node_dir(node_artifact_request("review-input"))
            if input_node_dir is None:
                self.fail("Expected input node directory to be created")
            assert (input_node_dir / "input_round1.md").read_text(
                encoding="utf-8"
            ) == "Raw findings from file"
            assert (
                output.results_dir / build_result_filename("review-input")
            ).read_text(encoding="utf-8") == "Raw findings from file"
            assert "Raw findings from file" in invoker.calls[0]["prompt"]

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

            with pytest.raises(
                WorkflowExecutionError,
                match="Resolved input content for node 'empty-input' is empty after preflight assembly.",
            ):
                await execute_workflow(config, workflow, output, invoker=invoker)

            assert invoker.calls == []

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
                pytest.raises(WorkflowExecutionError) as raised,
            ):
                await execute_workflow(config, workflow, output, invoker=invoker)

            failure_message = str(raised.value)
            assert "Workflow 'dag.empty.prompt' failed:" in failure_message
            assert "- failed: empty-prompt" in failure_message
            assert (
                "Resolved executor prompt for node 'empty-prompt' is empty"
                in failure_message
            )
            assert (
                "- blocked: blocked (unsatisfied dependencies: empty-prompt)"
                in failure_message
            )
            assert invoker.calls == []

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

            with pytest.raises(WorkflowExecutionError) as raised:
                await execute_workflow(config, workflow, output, invoker=invoker)

            failure_message = str(raised.value)
            assert "Workflow 'dag.invalid.findings' failed:" in failure_message
            assert "- failed: findings-node" in failure_message
            assert "Expected exactly one findings block" in failure_message
            assert (
                "- blocked: blocked (unsatisfied dependencies: findings-node)"
                in failure_message
            )
