import json
import tempfile
import unittest
from pathlib import Path

from crewplane.architecture.contracts import EventType
from crewplane.artifacts import OutputManager
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
)
from crewplane.observability.events import ExecutionEvent
from crewplane.runtime.execution.common import (
    ExecutionTelemetry,
)
from crewplane.runtime.execution.consensus import (
    extract_verdict,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    TaskOutputInvoker,
    execute_sequential_stage,
    review_inbox_path,
    review_loop_status_path,
    review_output,
    review_state_path,
)


class ExecutorReviewLoopContractsTests(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_reviewer_approval_persists_state_without_inbox(
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
                id="review.node.immediate.approval",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
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
                        review_output(verdict="NO_FINDINGS"),
                    ]
                ),
            )

            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")

            state_payload = json.loads(
                review_state_path(node_dir, "review_reviewer_0", 1).read_text(
                    encoding="utf-8"
                )
            )
            assert state_payload["approved"]
            assert state_payload["evaluation_kind"] == "structured"
            assert not review_inbox_path(node_dir, 1).exists()

    async def test_malformed_structured_reviewer_output_persists_nonapproval_state(
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
                id="review.node.malformed",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                ],
            )
            malformed_review = "\n".join(
                [
                    "## Major Issues",
                    "None",
                    "",
                    "## Minor Issues",
                    "",
                    "## Nitpicks",
                    "None",
                    "",
                    "---",
                    "VERDICT: NO_FINDINGS",
                    "",
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=MockAgentInvoker(outputs=["executor output", malformed_review]),
            )

            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")

            normalized_output = (node_dir / "review_reviewer_0_round1.md").read_text(
                encoding="utf-8"
            )
            raw_output = (node_dir / "review_reviewer_0_round1.raw.txt").read_text(
                encoding="utf-8"
            )
            state_payload = json.loads(
                review_state_path(node_dir, "review_reviewer_0", 1).read_text(
                    encoding="utf-8"
                )
            )

            assert raw_output == malformed_review
            assert "Unstructured Reviewer Feedback" in normalized_output
            assert state_payload["evaluation_kind"] == "unstructured_feedback"
            assert state_payload["original_verdict"] == "NO_FINDINGS"
            assert state_payload["major_issues"] == "None"
            assert state_payload["minor_issues"] == "None"
            assert state_payload["unresolved_issue_count"] == 0
            assert "## Minor Issues" in state_payload["unstructured_feedback"]
            assert review_inbox_path(node_dir, 1).exists()
            inbox_text = review_inbox_path(node_dir, 1).read_text(encoding="utf-8")
            assert "#### Unstructured Feedback" in inbox_text
            assert "#### Minor Issues" not in inbox_text

    async def test_missing_major_section_nits_only_review_reaches_consensus(
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
                id="review.node.repaired.nits",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                ],
            )
            reviewer_output = "\n".join(
                [
                    "## Minor Issues",
                    "None",
                    "",
                    "## Nitpicks",
                    "- Tighten the closing sentence.",
                    "",
                    "---",
                    "VERDICT: NITS_ONLY",
                    "",
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=MockAgentInvoker(outputs=["executor output", reviewer_output]),
                telemetry=ExecutionTelemetry(
                    workflow_name="workflow",
                    run_id="run-1",
                    event_sink=events.append,
                ),
            )

            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")
            state_payload = json.loads(
                review_state_path(node_dir, "review_reviewer_0", 1).read_text(
                    encoding="utf-8"
                )
            )
            metadata = json.loads(
                (node_dir / "review_reviewer_0_round1.review.json").read_text(
                    encoding="utf-8"
                )
            )

            assert state_payload["approved"]
            assert state_payload["verdict"] == "NITS_ONLY"
            assert state_payload["major_issues"] == "None"
            assert state_payload["minor_issues"] == "None"
            assert state_payload["unresolved_issue_count"] == 0
            assert not review_inbox_path(node_dir, 1).exists()
            assert "missing Major Issues section" in metadata["warnings"][0]
            warning_events = [
                event
                for event in events
                if event.event_type == EventType.RUNTIME_LOG
                and event.payload.operation == "review_output_normalization"
            ]
            assert len(warning_events) == 1

    async def test_empty_reviewer_output_persists_failure_state_and_honors_policy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                settings=Settings(sequential_consensus_on_exhaustion="fatal"),
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.node.empty.reviewer",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                depth=1,
                continue_on_failure=True,
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                ],
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=MockAgentInvoker(outputs=["executor output", "  "]),
            )

            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")
            state_payload = json.loads(
                review_state_path(node_dir, "review_reviewer_0", 1).read_text(
                    encoding="utf-8"
                )
            )

            assert state_payload["evaluation_kind"] == "reviewer_failure"
            assert state_payload["failure_kind"] == "missing_review_content"
            assert not (node_dir / "review_reviewer_0_round1.review.json").exists()
            assert not review_inbox_path(node_dir, 1).exists()

    async def test_empty_peer_reviewer_does_not_allow_partial_consensus(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                settings=Settings(sequential_consensus_on_exhaustion="fatal"),
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review-a": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                    "review-b": AgentConfig(cli_cmd=["mock"], default_model="m3"),
                },
            )
            node = WorkflowNode(
                id="review.node.partial.failure",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                depth=1,
                continue_on_failure=True,
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review-a", role=ProviderRole.REVIEWER),
                    ProviderSpec(provider="review-b", role=ProviderRole.REVIEWER),
                ],
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=TaskOutputInvoker(
                    outputs_by_task_id={
                        "exec_executor_0": "executor output",
                        "review-a_reviewer_0": review_output(verdict="NO_FINDINGS"),
                        "review-b_reviewer_1": "  ",
                    },
                ),
            )

            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")
            status_payload = json.loads(
                review_loop_status_path(node_dir).read_text(encoding="utf-8")
            )
            review_a_state = json.loads(
                review_state_path(node_dir, "review-a_reviewer_0", 1).read_text(
                    encoding="utf-8"
                )
            )
            review_b_state = json.loads(
                review_state_path(node_dir, "review-b_reviewer_1", 1).read_text(
                    encoding="utf-8"
                )
            )

            assert not status_payload["consensus_reached"]
            assert status_payload["continued_after_consensus_exhaustion"]
            assert review_a_state["approved"]
            assert review_b_state["evaluation_kind"] == "reviewer_failure"
            assert review_b_state["failure_kind"] == "missing_review_content"
            assert not review_inbox_path(node_dir, 1).exists()

    async def test_multi_provider_sequential_normalizes_reviewer_preamble(
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
                id="review.node.preamble",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                ],
            )
            reviewer_output = "\n".join(
                [
                    "Now let me check the remaining changed files.",
                    "",
                    "I have compiled my findings.",
                    "",
                    review_output(
                        nitpicks="- Tighten the naming in the final summary section",
                        verdict="NITS_ONLY",
                    ),
                ]
            )
            invoker = MockAgentInvoker(outputs=["executor output", reviewer_output])
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

            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")
            normalized_output = (node_dir / "review_reviewer_0_round1.md").read_text(
                encoding="utf-8"
            )
            raw_output = (node_dir / "review_reviewer_0_round1.raw.txt").read_text(
                encoding="utf-8"
            )
            metadata = json.loads(
                (node_dir / "review_reviewer_0_round1.review.json").read_text(
                    encoding="utf-8"
                )
            )

            assert extract_verdict(normalized_output) == "NITS_ONLY"
            assert raw_output.startswith("Now let me check")
            assert metadata["had_leading_text"]
            warning_events = [
                event
                for event in events
                if event.event_type == EventType.RUNTIME_LOG
                and event.payload.operation == "review_output_normalization"
            ]
            assert warning_events == []
