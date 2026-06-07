import json
import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.core.config import AgentConfig, Config, Settings
from orchestrator_cli.core.versions import CONFIG_SCHEMA_VERSION
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
)
from orchestrator_cli.observability.events import ExecutionEvent
from orchestrator_cli.runtime.execution.common import (
    ExecutionTelemetry,
)
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    audit_round_dir,
    execute_sequential_stage,
    review_inbox_path,
    review_output,
    review_state_path,
)


class ExecutorReviewLoopRoundControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_fresh_audit_stops_later_audit_rounds_early(self) -> None:
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
                id="review.node.clean.fresh.audit",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=2,
                audit_rounds=4,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "executor output",
                    review_output(verdict="NO_FINDINGS"),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertEqual(len(invoker.calls), 2)
            self.assertEqual(invoker.calls[0]["audit_round_num"], 1)
            self.assertEqual(invoker.calls[1]["audit_round_num"], 1)
            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            self.assertTrue(audit_round_dir(node_dir, 1).exists())
            self.assertFalse(audit_round_dir(node_dir, 2).exists())

    async def test_remediation_approval_stops_current_audit_round_early(
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
                id="review.node.remediation.stop",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=2,
                audit_rounds=2,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "executor output round 1",
                    review_output(
                        major="- Fix missing regression coverage",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "executor output round 2",
                    review_output(verdict="NO_FINDINGS"),
                    review_output(verdict="NO_FINDINGS"),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertEqual(len(invoker.calls), 5)
            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            audit_round_1 = audit_round_dir(node_dir, 1)
            audit_round_2 = audit_round_dir(node_dir, 2)
            self.assertFalse((audit_round_1 / "exec_executor_0_round3.md").exists())
            self.assertEqual(
                (audit_round_2 / "exec_executor_0_round1.md").read_text(
                    encoding="utf-8"
                ),
                "executor output round 2",
            )
            round_2_calls = [
                call for call in invoker.calls if call["audit_round_num"] == 2
            ]
            self.assertEqual(len(round_2_calls), 1)
            self.assertEqual(round_2_calls[0]["round_num"], 1)

    async def test_later_fresh_audit_can_reopen_remediation_with_new_issue(
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
                id="review.node.reopen",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                audit_rounds=2,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "candidate v1",
                    review_output(
                        major="- Fix auth edge case",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "candidate v2",
                    review_output(verdict="NO_FINDINGS"),
                    review_output(
                        minor="- Add a concurrency regression test",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "candidate v3",
                    review_output(verdict="NO_FINDINGS"),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertEqual(len(invoker.calls), 7)
            fresh_audit_prompt = str(invoker.calls[4]["prompt"])
            self.assertNotIn("Previous unresolved review state:", fresh_audit_prompt)
            remediation_prompt = str(invoker.calls[5]["prompt"])
            self.assertIn("Add a concurrency regression test", remediation_prompt)
            self.assertNotIn("Fix auth edge case", remediation_prompt)

    async def test_exhausted_audit_round_continues_to_fresh_next_round_without_carry_forward(
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
                id="review.node.exhausted.audit",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                audit_rounds=2,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "candidate v1",
                    review_output(
                        major="- Add validation for empty payloads",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "candidate v2",
                    review_output(
                        major="- Still missing validation for empty payloads",
                        verdict="CHANGES_REQUESTED",
                    ),
                    review_output(verdict="NO_FINDINGS"),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertEqual(len(invoker.calls), 5)
            fresh_audit_prompt = str(invoker.calls[4]["prompt"])
            self.assertNotIn("Previous unresolved review state:", fresh_audit_prompt)
            self.assertNotIn("Still missing validation", fresh_audit_prompt)

            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            self.assertEqual(
                (audit_round_dir(node_dir, 2) / "exec_executor_0_round1.md").read_text(
                    encoding="utf-8"
                ),
                "candidate v2",
            )

    async def test_single_provider_sequential_does_not_create_review_state_dir(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                agents={"exec": AgentConfig(cli_cmd=["mock"], default_model="m1")},
            )
            node = WorkflowNode(
                id="single.provider.no.review.state",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Run this.")],
                depth=2,
                providers=[ProviderSpec(provider="exec", role="executor")],
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=MockAgentInvoker(outputs=["round 1", "round 2"]),
            )

            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            self.assertFalse((node_dir / "review-state").exists())

    async def test_multi_provider_sequential_persists_review_state_and_inbox(
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
                id="review.node.state",
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
                        major="- Add missing regression tests",
                        nitpicks="- Consider tightening the title",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "executor output round 2",
                    review_output(verdict="NO_FINDINGS"),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")

            round_1_state = json.loads(
                review_state_path(node_dir, "review_reviewer_0", 1).read_text(
                    encoding="utf-8"
                )
            )
            round_2_state = json.loads(
                review_state_path(node_dir, "review_reviewer_0", 2).read_text(
                    encoding="utf-8"
                )
            )
            inbox_round_1 = review_inbox_path(node_dir, 1)
            inbox_round_2 = review_inbox_path(node_dir, 2)

            self.assertEqual(
                round_1_state["major_issues"], "- Add missing regression tests"
            )
            self.assertEqual(
                round_1_state["nitpicks"], "- Consider tightening the title"
            )
            self.assertEqual(len(round_1_state["unresolved_fingerprints"]), 1)
            self.assertEqual(round_1_state["unresolved_issue_count"], 1)
            self.assertTrue(round_2_state["approved"])
            self.assertEqual(round_2_state["evaluation_kind"], "structured")
            self.assertTrue(inbox_round_1.exists())
            self.assertFalse(inbox_round_2.exists())

            inbox_text = inbox_round_1.read_text(encoding="utf-8")
            self.assertIn("Add missing regression tests", inbox_text)
            self.assertNotIn(
                "Consider tightening the title", invoker.calls[2]["prompt"]
            )
            self.assertIn(
                "Previous unresolved review state:", invoker.calls[2]["prompt"]
            )
            self.assertIn("Previous canonical candidate:", invoker.calls[2]["prompt"])
            self.assertIn("executor output round 1", invoker.calls[2]["prompt"])
            self.assertLess(
                invoker.calls[2]["prompt"].index("Previous canonical candidate:"),
                invoker.calls[2]["prompt"].index("Previous unresolved review state:"),
            )

    async def test_remediation_executor_prompt_reuses_previous_canonical_candidate(
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
                id="review.node.previous.candidate",
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
                    "# Candidate v1\n\nOriginal design body.",
                    review_output(
                        minor="- Add the missing recovery owner.",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "# Candidate v2\n\nUpdated design body.",
                    review_output(verdict="NO_FINDINGS"),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            initial_prompt = invoker.calls[0]["prompt"]
            remediation_prompt = invoker.calls[2]["prompt"]

            self.assertIn(
                "Start directly with the candidate document title",
                initial_prompt,
            )
            self.assertIn("Do not include progress notes", initial_prompt)
            self.assertIn("Previous canonical candidate:", remediation_prompt)
            self.assertIn("# Candidate v1", remediation_prompt)
            self.assertIn("Original design body.", remediation_prompt)
            self.assertLess(
                remediation_prompt.index("Previous canonical candidate:"),
                remediation_prompt.index("Previous unresolved review state:"),
            )

    async def test_remediation_previous_candidate_emits_budget_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=CONFIG_SCHEMA_VERSION,
                settings=Settings(token_budget={"warn_threshold_chars": 10}),
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.node.previous.candidate.budget",
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
                    "# Candidate v1\n\nThis previous candidate is intentionally long.",
                    review_output(
                        minor="- Add the missing recovery owner.",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "# Candidate v2\n\nUpdated design body.",
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

            warning_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.operation == "prompt_budget_warning"
            ]
            self.assertEqual(len(warning_events), 1)
            self.assertIn("previous canonical candidate", warning_events[0].message)
            self.assertEqual(
                warning_events[0].attributes["upstream_artifact_name"],
                "previous_canonical_candidate",
            )

    async def test_remediation_previous_candidate_respects_fail_budget(self) -> None:
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
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.node.previous.candidate.fail.budget",
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
                    "# Candidate v1\n\nThis previous candidate is intentionally long.",
                    review_output(
                        minor="- Add the missing recovery owner.",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "unused",
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            with self.assertRaisesRegex(RuntimeError, "previous canonical candidate"):
                await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertEqual(len(invoker.calls), 2)
