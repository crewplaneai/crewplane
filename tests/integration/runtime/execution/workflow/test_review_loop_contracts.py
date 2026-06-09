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
from orchestrator_cli.runtime.execution.consensus import (
    extract_verdict,
)
from orchestrator_cli.versions import CONFIG_SCHEMA_VERSION
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    OptionalOutputInvoker,
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
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.node.immediate.approval",
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
                        review_output(verdict="NO_FINDINGS"),
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
            self.assertTrue(state_payload["approved"])
            self.assertEqual(state_payload["evaluation_kind"], "structured")
            self.assertFalse(review_inbox_path(node_dir, 1).exists())

    async def test_malformed_structured_reviewer_output_persists_nonapproval_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.node.malformed",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
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

            node_dir = output.get_stage_dir(node.id)
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

            self.assertEqual(raw_output, malformed_review)
            self.assertIn(
                "malformed structured review block",
                normalized_output,
            )
            self.assertEqual(
                state_payload["evaluation_kind"], "unstructured_nonapproval"
            )
            self.assertEqual(state_payload["original_verdict"], "NO_FINDINGS")
            self.assertTrue(review_inbox_path(node_dir, 1).exists())

    async def test_multi_provider_sequential_normalizes_reviewer_preamble(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )

            node = WorkflowNode(
                id="review.node.preamble",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
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

            node_dir = output.get_stage_dir(node.id)
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

            self.assertEqual(extract_verdict(normalized_output), "NITS_ONLY")
            self.assertTrue(raw_output.startswith("Now let me check"))
            self.assertTrue(metadata["had_leading_text"])
            warning_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_output_normalization"
            ]
            self.assertEqual(warning_events, [])

    async def test_multi_provider_sequential_defaults_to_continue_on_no_consensus(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="no.consensus.default",
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
                    "executor output",
                    review_output(
                        minor="- Add the missing test assertions",
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
            self.assertEqual(len(invoker.calls), 4)
            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            status_payload = json.loads(
                review_loop_status_path(node_dir).read_text(encoding="utf-8")
            )
            self.assertTrue(status_payload["continued_after_consensus_exhaustion"])
            self.assertEqual(status_payload["invalid_candidate_round_count"], 0)
            self.assertEqual(status_payload["no_progress_round_count"], 0)
            exhaustion_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_loop_consensus_exhausted"
            ]
            self.assertEqual(len(exhaustion_events), 1)

    async def test_multi_provider_sequential_defaults_to_one_remediation_cycle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.loop.default.depth",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "executor output",
                    review_output(
                        major="- Fix the remaining bug before approval",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "executor output round 2 should not run",
                    "reviewer output round 2 should not run",
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertEqual(len(invoker.calls), 4)

    async def test_multi_provider_sequential_skips_reviewer_on_empty_remediation_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.loop.invalid.remediation",
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
                        major="- Fix the missing error-handling branch",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "   ",
                    "executor output round 3",
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

            self.assertEqual(len(invoker.calls), 5)
            self.assertNotIn(
                ("reviewer", 2),
                [(call["role"], call["round_num"]) for call in invoker.calls],
            )
            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            status_payload = json.loads(
                review_loop_status_path(node_dir).read_text(encoding="utf-8")
            )
            self.assertEqual(status_payload["invalid_candidate_round_count"], 1)
            self.assertEqual(status_payload["no_progress_round_count"], 0)
            self.assertEqual(status_payload["final_local_round_num"], 3)
            self.assertEqual(
                status_payload["canonical_executor_outputs"][0]["path"],
                "exec_executor_0_round3.md",
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
                "invalid_candidate.empty",
            )

    async def test_multi_provider_sequential_skips_reviewer_on_missing_remediation_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.loop.missing.remediation",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=2,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            invoker = OptionalOutputInvoker(
                outputs=[
                    "executor output round 1",
                    review_output(
                        major="- Fix the missing error-handling branch",
                        verdict="CHANGES_REQUESTED",
                    ),
                    None,
                    "executor output round 3",
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
            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            self.assertFalse((node_dir / "exec_executor_0_round2.md").exists())
            status_payload = json.loads(
                review_loop_status_path(node_dir).read_text(encoding="utf-8")
            )
            self.assertEqual(status_payload["invalid_candidate_round_count"], 1)
            invalid_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_loop_invalid_candidate"
            ]
            self.assertEqual(len(invalid_events), 1)
            self.assertEqual(
                invalid_events[0].payload.attributes["reason"],
                "invalid_candidate.empty",
            )

    async def test_multi_provider_sequential_skips_reviewer_on_unchanged_remediation_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.loop.no.progress",
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
                        major="- Fix the missing regression path",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "executor output round 1",
                    "executor output round 3 with changes",
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

            self.assertEqual(len(invoker.calls), 5)
            self.assertNotIn(
                ("reviewer", 2),
                [(call["role"], call["round_num"]) for call in invoker.calls],
            )
            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            status_payload = json.loads(
                review_loop_status_path(node_dir).read_text(encoding="utf-8")
            )
            self.assertEqual(status_payload["invalid_candidate_round_count"], 0)
            self.assertEqual(status_payload["no_progress_round_count"], 1)
            no_progress_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_loop_no_progress"
            ]
            self.assertEqual(len(no_progress_events), 1)

    async def test_multi_provider_sequential_accepts_mixed_commentary_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.loop.mixed.commentary",
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
                            "Updated as requested.",
                            "",
                            "# Revised Design",
                            "",
                            "- Add the rollback path.",
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
