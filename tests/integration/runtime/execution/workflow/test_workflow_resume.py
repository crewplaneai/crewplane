from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crewplane.artifacts import OutputManager
from crewplane.core.config import AgentConfig, Config
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.events import ExecutionEvent
from crewplane.observability.events.payloads import RuntimeLogEventPayload
from crewplane.version import SCHEMA_VERSION
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    execute_workflow,
)


class WorkflowResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resumed_nodes_are_marked_succeeded_before_scheduling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={"alpha": AgentConfig(cli_cmd=["mock"], default_model="model")},
            )
            workflow = WorkflowPlan(
                name="resume.workflow",
                nodes=[
                    WorkflowNode(
                        id="a",
                        mode="sequential",
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
                        prompt_segments=[
                            PromptSegment(role=PromptSegmentRole.SHARED, content="A")
                        ],
                    ),
                    WorkflowNode(
                        id="b",
                        mode="sequential",
                        needs=["a"],
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                        ],
                        prompt_segments=[
                            PromptSegment(role=PromptSegmentRole.SHARED, content="B")
                        ],
                    ),
                ],
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)
            invoker = MockAgentInvoker(outputs=["b result"])
            events: list[ExecutionEvent] = []

            await execute_workflow(
                config,
                workflow,
                output,
                invoker,
                event_sink=events.append,
                suppress_progress_output=True,
                workflow_identity=".crewplane/workflows/resume.task.md",
                resumed_node_ids=("a",),
            )

            self.assertEqual([call["node_id"] for call in invoker.calls], ["b"])
            self.assertIn(
                "b result",
                output.get_stage_output_path("b").read_text(encoding="utf-8"),
            )
            node_events = [
                (event.event_type, event.context.node_id)
                for event in events
                if event.event_type in {"node_started", "node_finished"}
            ]
            self.assertEqual(
                node_events[:3],
                [
                    ("node_started", "a"),
                    ("node_finished", "a"),
                    ("node_started", "b"),
                ],
            )
            resumed_logs = [
                event
                for event in events
                if isinstance(event.payload, RuntimeLogEventPayload)
                and event.payload.operation == "node_resumed"
            ]
            self.assertEqual([event.context.node_id for event in resumed_logs], ["a"])
