import json
import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.core.config import AgentConfig, Config
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
)
from orchestrator_cli.observability.events import ExecutionEvent
from orchestrator_cli.runtime.execution.common import (
    ExecutionTelemetry,
)
from orchestrator_cli.version import SCHEMA_VERSION
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    execute_sequential_stage,
    review_inbox_path,
    review_output,
    review_state_path,
)


class ExecutorReviewLoopInboxProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_reviewer_inbox_groups_unresolved_findings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review-a": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                    "review-b": AgentConfig(cli_cmd=["mock"], default_model="m3"),
                },
            )
            node = WorkflowNode(
                id="review.node.multi.reviewer",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review-a", role="reviewer"),
                    ProviderSpec(provider="review-b", role="reviewer"),
                ],
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=MockAgentInvoker(
                    outputs=[
                        "executor output",
                        review_output(
                            major="- Add regression coverage",
                            verdict="CHANGES_REQUESTED",
                        ),
                        review_output(
                            minor="- Update the docs example",
                            verdict="CHANGES_REQUESTED",
                        ),
                    ]
                ),
            )

            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            inbox_text = review_inbox_path(node_dir, 1).read_text(encoding="utf-8")
            self.assertIn("review-a reviewer (review-a_reviewer_0)", inbox_text)
            self.assertIn("review-b reviewer (review-b_reviewer_1)", inbox_text)
            self.assertIn("Add regression coverage", inbox_text)
            self.assertIn("Update the docs example", inbox_text)

    async def test_multi_executor_inbox_tracks_current_and_previous_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec-a": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "exec-b": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m3"),
                },
            )
            node = WorkflowNode(
                id="review.node.multi.executor.state",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=2,
                providers=[
                    ProviderSpec(provider="exec-a", role="executor"),
                    ProviderSpec(provider="exec-b", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "executor a round 1",
                    "executor b round 1",
                    review_output(
                        major="- Keep iterating",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "executor a round 2 changed",
                    "executor b round 2 changed",
                    review_output(
                        major="- Keep iterating",
                        verdict="CHANGES_REQUESTED",
                    ),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=invoker,
                telemetry=ExecutionTelemetry(
                    workflow_name="workflow",
                    run_id="run-1",
                    event_sink=events.append,
                ),
            )

            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            inbox_text = review_inbox_path(node_dir, 2).read_text(encoding="utf-8")

            self.assertIn(
                f"current-output: {node_dir / 'exec-a_executor_0_round2.md'}",
                inbox_text,
            )
            self.assertIn(
                f"previous-output: {node_dir / 'exec-a_executor_0_round1.md'}",
                inbox_text,
            )
            self.assertIn(
                f"current-output: {node_dir / 'exec-b_executor_1_round2.md'}",
                inbox_text,
            )
            self.assertIn(
                f"previous-output: {node_dir / 'exec-b_executor_1_round1.md'}",
                inbox_text,
            )
            stall_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_stall_detection"
            ]
            self.assertEqual(len(stall_events), 1)
            self.assertIn("executor output changed", stall_events[0].payload.message)

    async def test_multi_provider_sequential_warns_on_unchanged_output_no_progress(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.node.no.progress",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=2,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            output = OutputManager("workflow", base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=MockAgentInvoker(
                    outputs=[
                        "executor output",
                        review_output(
                            minor="- Add missing assertions",
                            verdict="CHANGES_REQUESTED",
                        ),
                        "executor output",
                    ]
                ),
                telemetry=ExecutionTelemetry(
                    workflow_name="workflow",
                    run_id="run-1",
                    event_sink=events.append,
                ),
            )

            no_progress_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_loop_no_progress"
            ]
            self.assertEqual(len(no_progress_events), 1)

    async def test_multi_provider_sequential_warns_on_wording_drift_same_reference(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.node.wording.drift",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            output = OutputManager("workflow", base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=MockAgentInvoker(
                    outputs=[
                        "executor output round 1",
                        review_output(
                            major=(
                                "- Fix breaker accounting in "
                                "`apps/llm/domain/services/generation_service.py:100-120`."
                            ),
                            verdict="CHANGES_REQUESTED",
                        ),
                        "executor output round 2 changed",
                        review_output(
                            major=(
                                "- Stream breaker handling in "
                                "`apps/llm/domain/services/generation_service.py:140-170` "
                                "is still incorrect."
                            ),
                            verdict="CHANGES_REQUESTED",
                        ),
                    ]
                ),
                telemetry=ExecutionTelemetry(
                    workflow_name="workflow",
                    run_id="run-1",
                    event_sink=events.append,
                ),
            )

            stall_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_stall_detection"
            ]
            self.assertEqual(len(stall_events), 1)
            self.assertEqual(
                stall_events[0].payload.attributes["current_unresolved_issue_count"],
                1,
            )
            self.assertEqual(
                stall_events[0].payload.attributes["repeated_fingerprint_count"], 1
            )
            self.assertNotIn("repeated_issue_count", stall_events[0].payload.attributes)

    async def test_stall_warning_reports_issue_count_not_fingerprint_count(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.node.issue.count",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            repeated_issue = (
                "- Fix alpha beta gamma delta epsilon zeta in `src/a.py:10`, "
                "`src/b.py:20`, and `src/c.py:30`."
            )
            output = OutputManager("workflow", base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=MockAgentInvoker(
                    outputs=[
                        "executor output round 1",
                        review_output(
                            major=repeated_issue,
                            verdict="CHANGES_REQUESTED",
                        ),
                        "executor output round 2 changed",
                        review_output(
                            major=repeated_issue,
                            verdict="CHANGES_REQUESTED",
                        ),
                    ]
                ),
                telemetry=ExecutionTelemetry(
                    workflow_name="workflow",
                    run_id="run-1",
                    event_sink=events.append,
                ),
            )

            stall_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_stall_detection"
            ]
            self.assertEqual(len(stall_events), 1)
            self.assertEqual(
                stall_events[0].payload.attributes["current_unresolved_issue_count"],
                1,
            )
            self.assertGreater(
                stall_events[0].payload.attributes["repeated_fingerprint_count"],
                1,
            )

    async def test_multi_provider_sequential_does_not_warn_for_unrelated_same_file_issue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.node.same.file.no.stall",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=2,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            output = OutputManager("workflow", base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=MockAgentInvoker(
                    outputs=[
                        "executor output round 1",
                        review_output(
                            major="- Add regression coverage in `src/app.py:10-20`.",
                            verdict="CHANGES_REQUESTED",
                        ),
                        "executor output round 2 changed",
                        review_output(
                            major="- Rename the helper in `src/app.py:200-220` for clarity.",
                            verdict="CHANGES_REQUESTED",
                        ),
                    ]
                ),
                telemetry=ExecutionTelemetry(
                    workflow_name="workflow",
                    run_id="run-1",
                    event_sink=events.append,
                ),
            )

            stall_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_stall_detection"
            ]
            self.assertEqual(stall_events, [])

    async def test_inferred_reviewer_output_persists_review_state_and_terminal_inbox(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.node.inferred",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=MockAgentInvoker(
                    outputs=[
                        "executor output",
                        "Needs changes before merge.",
                    ]
                ),
            )

            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")

            state_payload = json.loads(
                review_state_path(node_dir, "review_reviewer_0", 1).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                state_payload["evaluation_kind"], "plain_language_changes_requested"
            )
            self.assertTrue(review_inbox_path(node_dir, 1).exists())
