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
    ArtifactDriftInvoker,
    MockAgentInvoker,
    execute_sequential_stage,
    review_loop_status_path,
    review_output,
)


class ExecutorReviewLoopCandidateFilteringTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_provider_sequential_skips_reviewer_on_redirect_only_candidate(
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
                id="review.loop.redirect.only",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=2,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "executor output round 1",
                    review_output(
                        major="- Fix the missing resilience branch",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "See exec_executor_0_round1.md for the updated design.",
                    "executor output round 3 with fixes",
                    review_output(verdict="NO_FINDINGS"),
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

            self.assertNotIn(
                ("reviewer", 2),
                [(call["role"], call["round_num"]) for call in invoker.calls],
            )
            invalid_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_loop_invalid_candidate"
            ]
            self.assertEqual(len(invalid_events), 1)
            self.assertEqual(
                invalid_events[0].payload.attributes["reason"],
                "invalid_candidate.redirected",
            )

    async def test_multi_provider_sequential_skips_reviewer_on_redirect_status_note(
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
                id="review.loop.redirect.status.note",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=2,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "executor output round 1",
                    review_output(
                        major="- Fix the missing resilience branch",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "Updated exec_executor_0_round1.md with the changes.\n\nDone.",
                    "executor output round 3 with fixes",
                    review_output(verdict="NO_FINDINGS"),
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

            self.assertNotIn(
                ("reviewer", 2),
                [(call["role"], call["round_num"]) for call in invoker.calls],
            )
            invalid_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_loop_invalid_candidate"
            ]
            self.assertEqual(len(invalid_events), 1)
            self.assertEqual(
                invalid_events[0].payload.attributes["reason"],
                "invalid_candidate.redirected",
            )

    async def test_multi_provider_sequential_accepts_referenced_candidate_with_substantive_body(
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
                id="review.loop.referenced.commentary",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "\n".join(
                        [
                            "Updated exec_executor_0_round1.md with the revised design.",
                            "",
                            "The revised design now adds the rollback path, keeps review",
                            "state isolated by audit round, and records the canonical",
                            "candidate directly in the current response.",
                        ]
                    ),
                    review_output(verdict="NO_FINDINGS"),
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

            invalid_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_loop_invalid_candidate"
            ]
            self.assertEqual(invalid_events, [])

    async def test_multi_provider_sequential_retries_fresh_audit_when_no_candidate_exists(
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
                id="review.loop.no.candidate",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                audit_rounds=2,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            invoker = MockAgentInvoker(outputs=["   ", "   "])
            output = OutputManager("workflow", base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            with self.assertRaisesRegex(RuntimeError, "valid canonical candidate"):
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

            self.assertEqual(len(invoker.calls), 2)
            self.assertTrue(all(call["role"] == "executor" for call in invoker.calls))
            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            status_payload = json.loads(
                review_loop_status_path(node_dir).read_text(encoding="utf-8")
            )
            self.assertEqual(status_payload["invalid_candidate_round_count"], 2)
            self.assertEqual(status_payload["canonical_executor_outputs"], [])
            no_candidate_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_loop_no_canonical_candidate"
            ]
            self.assertEqual(len(no_candidate_events), 1)

    async def test_multi_provider_sequential_warns_on_executor_artifact_drift(
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
                id="review.loop.drift.warning",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            output = OutputManager("workflow", base_dir=tmp_path)
            node_dir = output.create_stage_dir(node.id)
            invoker = ArtifactDriftInvoker(
                outputs=[
                    "executor output round 1",
                    review_output(
                        major="- Fix the unhappy path",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "executor output round 2",
                    review_output(verdict="NO_FINDINGS"),
                ],
                mutations_by_call={
                    2: [
                        (
                            node_dir / "exec_executor_0_round1.md",
                            "mutated prior artifact",
                        )
                    ]
                },
            )
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

            drift_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_loop_artifact_drift"
                and event.payload.level == "warning"
            ]
            self.assertEqual(len(drift_events), 1)
            status_payload = json.loads(
                review_loop_status_path(node_dir).read_text(encoding="utf-8")
            )
            self.assertEqual(status_payload["artifact_drift_warning_count"], 1)
            self.assertEqual(
                status_payload["canonical_executor_outputs"][0]["path"],
                "exec_executor_0_round2.md",
            )
