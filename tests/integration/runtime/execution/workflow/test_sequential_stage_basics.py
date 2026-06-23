import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crewplane.artifacts import OutputManager, safe_artifact_name
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
from crewplane.observability.persistent import PersistentRunLogger
from crewplane.observability.types import RunContext
from crewplane.runtime.execution.common import (
    ExecutionTelemetry,
)
from crewplane.runtime.execution.fragment_assembler import ResolvedPrompt
from crewplane.version import SCHEMA_VERSION
from tests.helpers.observability import topology_from_workflow
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    FailingLogOutputManager,
    MockAgentInvoker,
    ParallelReviewerTimingInvoker,
    TimedTaskOutputInvoker,
    execute_sequential_stage,
    review_loop_status_path,
    review_output,
)


class ExecutorSequentialStageBasicsTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_provider_sequential_respects_depth_and_sanitizes_filename(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "solo/provider": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="test-model",
                    )
                },
            )

            node = WorkflowNode(
                id="single.provider.node",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
                depth=3,
                providers=[
                    ProviderSpec(provider="solo/provider", role=ProviderRole.EXECUTOR)
                ],
            )
            invoker = MockAgentInvoker(outputs=["one", "two", "three"])
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")

            safe_provider = safe_artifact_name("solo/provider")
            produced_files = sorted(path.name for path in node_dir.glob("*.md"))
            self.assertEqual(
                produced_files,
                [
                    f"{safe_provider}_executor_0_round1.md",
                    f"{safe_provider}_executor_0_round2.md",
                    f"{safe_provider}_executor_0_round3.md",
                ],
            )

    async def test_single_provider_sequential_rerenders_prompt_per_round(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={"exec": AgentConfig(cli_cmd=["mock"])},
            )
            node = WorkflowNode(
                id="single.provider.rerender",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
                depth=3,
                providers=[ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR)],
            )
            invoker = MockAgentInvoker(outputs=["one", "two", "three"])
            output = OutputManager("workflow", base_dir=tmp_path)
            candidate_source_flags: list[bool] = []

            def resolve_prompt(*args, **kwargs) -> ResolvedPrompt:  # type: ignore[no-untyped-def]
                del args
                candidate_source = kwargs["workspace_candidate_source"]
                candidate_source_flags.append(candidate_source)
                return ResolvedPrompt(f"prompt candidate={candidate_source}")

            with patch(
                "crewplane.runtime.execution.sequential.resolve_prompt_with_output_budget_details",
                side_effect=resolve_prompt,
            ):
                await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertEqual(candidate_source_flags, [False, True, True])
            self.assertEqual(
                [call["prompt"] for call in invoker.calls],
                [
                    "prompt candidate=False",
                    "prompt candidate=True",
                    "prompt candidate=True",
                ],
            )

    async def test_reviewer_prompt_includes_stage_artifacts_path(self) -> None:
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
                id="review.node",
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
            invoker = MockAgentInvoker(
                outputs=[
                    "executor output",
                    review_output(verdict="NO_FINDINGS"),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)
            self.assertEqual(len(invoker.calls), 2)

            reviewer_prompt = invoker.calls[1]["prompt"]
            self.assertTrue(
                reviewer_prompt.startswith("You are acting only as a reviewer.")
            )
            self.assertIn("Do not modify files", reviewer_prompt)
            self.assertNotIn("Node artifacts directory:", reviewer_prompt)
            self.assertLess(
                reviewer_prompt.index("Task context:\nReview this."),
                reviewer_prompt.index("Current executor output(s):"),
            )
            self.assertGreater(
                reviewer_prompt.index(
                    "VERDICT: CHANGES_REQUESTED | NITS_ONLY | NO_FINDINGS"
                ),
                reviewer_prompt.index("Current executor output(s):"),
            )
            self.assertIn(
                "## Major Issues",
                reviewer_prompt,
            )
            self.assertIn("## Minor Issues", reviewer_prompt)
            self.assertIn("## Nitpicks", reviewer_prompt)

    async def test_sequential_review_loop_uses_role_scoped_authored_prompt_content(
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
                id="review.node.role.scoped",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(
                        role=PromptSegmentRole.SHARED, content="Shared context.\n"
                    ),
                    PromptSegment(
                        role=PromptSegmentRole.EXECUTOR, content="Executor delta.\n"
                    ),
                    PromptSegment(
                        role=PromptSegmentRole.REVIEWER, content="Reviewer delta.\n"
                    ),
                ],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
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
            executor_prompt = str(invoker.calls[0]["prompt"])
            reviewer_prompt = str(invoker.calls[1]["prompt"])

            self.assertIn("Shared context.", executor_prompt)
            self.assertIn("Executor delta.", executor_prompt)
            self.assertNotIn("Reviewer delta.", executor_prompt)

            self.assertIn(
                "Task context:\nShared context.\nReviewer delta.", reviewer_prompt
            )
            self.assertNotIn("Executor delta.", reviewer_prompt)

    async def test_single_provider_sequential_fails_when_resolved_executor_prompt_is_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={"exec": AgentConfig(cli_cmd=["mock"], default_model="m1")},
            )
            node = WorkflowNode(
                id="single.provider.empty.prompt",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(
                        role="shared", content="{{env:EMPTY_EXECUTOR_PROMPT}}"
                    )
                ],
                providers=[ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR)],
            )
            invoker = MockAgentInvoker(outputs=["unused"])
            output = OutputManager("workflow", base_dir=tmp_path)

            with (
                patch.dict("os.environ", {"EMPTY_EXECUTOR_PROMPT": ""}, clear=False),
                self.assertRaisesRegex(
                    RuntimeError,
                    "Resolved executor prompt for node 'single.provider.empty.prompt' is empty after fragment assembly.",
                ),
            ):
                await execute_sequential_stage(
                    config,
                    node,
                    output,
                    invoker=invoker,
                )

            self.assertEqual(invoker.calls, [])

    async def test_review_loop_fails_when_resolved_reviewer_prompt_is_empty(
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
                id="review.loop.empty.reviewer.prompt",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(
                        role=PromptSegmentRole.EXECUTOR,
                        content="Executor instructions.",
                    ),
                    PromptSegment(
                        role=ProviderRole.REVIEWER,
                        content="{{env:EMPTY_REVIEWER_PROMPT}}",
                    ),
                ],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = MockAgentInvoker(outputs=["unused"])
            output = OutputManager("workflow", base_dir=tmp_path)

            with (
                patch.dict("os.environ", {"EMPTY_REVIEWER_PROMPT": ""}, clear=False),
                self.assertRaisesRegex(
                    RuntimeError,
                    "Resolved reviewer prompt for node 'review.loop.empty.reviewer.prompt' is empty after fragment assembly.",
                ),
            ):
                await execute_sequential_stage(
                    config,
                    node,
                    output,
                    invoker=invoker,
                )

            self.assertEqual(invoker.calls, [])

    async def test_multi_executor_sequential_reviewer_sees_all_outputs_in_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec-1": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "exec-2": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m3"),
                },
            )

            node = WorkflowNode(
                id="review.node.multi.executor",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec-1", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="exec-2", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "executor one output",
                    "executor two output",
                    review_output(verdict="NO_FINDINGS"),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertEqual(len(invoker.calls), 3)
            self.assertEqual(invoker.calls[0]["prompt"], invoker.calls[1]["prompt"])

            reviewer_prompt = invoker.calls[2]["prompt"]
            self.assertIn("=== exec-1 executor output ===", reviewer_prompt)
            self.assertIn("=== exec-2 executor output ===", reviewer_prompt)
            self.assertLess(
                reviewer_prompt.index("=== exec-1 executor output ==="),
                reviewer_prompt.index("=== exec-2 executor output ==="),
            )
            self.assertIn("executor one output", reviewer_prompt)
            self.assertIn("executor two output", reviewer_prompt)

            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            self.assertIn(
                str(node_dir / "exec-1_executor_0_round1.md"),
                reviewer_prompt,
            )
            self.assertIn(
                str(node_dir / "exec-2_executor_1_round1.md"),
                reviewer_prompt,
            )

    async def test_multi_provider_sequential_stops_after_approved_review_cycle(
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
                id="review.node.qualified.lgtm",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                depth=3,
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "executor output round 1",
                    review_output(
                        major="- Add missing regression tests",
                        verdict="CHANGES_REQUESTED",
                    ),
                    "executor output round 2",
                    review_output(
                        nitpicks="- Consider tightening the section title",
                        verdict="NITS_ONLY",
                    ),
                    "executor output round 3 should not run",
                    "reviewer output round 3 should not run",
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertEqual(len(invoker.calls), 4)
            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            self.assertFalse((node_dir / "exec_executor_0_round3.md").exists())
            self.assertFalse((node_dir / "review_reviewer_0_round3.md").exists())
            second_round_reviewer_prompt = invoker.calls[3]["prompt"]
            self.assertIn(
                "Previous unresolved review state:", second_round_reviewer_prompt
            )
            self.assertIn("Add missing regression tests", second_round_reviewer_prompt)

    async def test_multi_provider_reviewers_run_in_parallel_within_local_round(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="exec-model"),
                    "review-a": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-a-model",
                    ),
                    "review-b": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-b-model",
                    ),
                },
            )
            node = WorkflowNode(
                id="review.node.parallel.reviewers",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review-a", role=ProviderRole.REVIEWER),
                    ProviderSpec(provider="review-b", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = ParallelReviewerTimingInvoker(reviewer_delay=0.1)
            output = OutputManager("workflow", base_dir=tmp_path)

            started_at = asyncio.get_running_loop().time()
            await execute_sequential_stage(config, node, output, invoker=invoker)
            elapsed = asyncio.get_running_loop().time() - started_at

            self.assertLess(elapsed, 0.18)
            review_a_started = invoker.started_at["review-a_reviewer_0"]
            review_b_started = invoker.started_at["review-b_reviewer_1"]
            self.assertLess(abs(review_a_started - review_b_started), 0.05)

    async def test_parallel_reviewer_metadata_persistence_does_not_warn_as_drift(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="exec-model"),
                    "review-a": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-a-model",
                    ),
                    "review-b": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-b-model",
                    ),
                },
            )
            node = WorkflowNode(
                id="review.node.parallel.metadata",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review-a", role=ProviderRole.REVIEWER),
                    ProviderSpec(provider="review-b", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = TimedTaskOutputInvoker(
                outputs_by_task_id={
                    "exec_executor_0": "executor output",
                    "review-a_reviewer_0": review_output(verdict="NO_FINDINGS"),
                    "review-b_reviewer_1": review_output(verdict="NO_FINDINGS"),
                },
                delays_by_task_id={"review-a_reviewer_0": 0.05},
            )
            output = OutputManager("workflow", base_dir=tmp_path)
            persistent_logger = PersistentRunLogger(output)
            persistent_logger.start(
                RunContext(
                    workflow_topology=topology_from_workflow(
                        WorkflowPlan(name="workflow", nodes=[node])
                    ),
                    run_id=output.run_id,
                    refresh_per_second=0,
                )
            )
            events: list[ExecutionEvent] = []

            def record_event(event: ExecutionEvent) -> None:
                events.append(event)
                persistent_logger.record_event(event)

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=invoker,
                telemetry=ExecutionTelemetry(
                    workflow_name="workflow",
                    run_id=output.run_id,
                    event_sink=record_event,
                ),
            )

            drift_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_loop_artifact_drift"
            ]
            self.assertEqual(drift_events, [])
            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            status_payload = json.loads(
                review_loop_status_path(node_dir).read_text(encoding="utf-8")
            )
            self.assertEqual(status_payload["artifact_drift_warning_count"], 0)

    async def test_parallel_reviewer_log_setup_failure_uses_invocation_lifecycle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="exec-model"),
                    "review-a": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-a-model",
                    ),
                    "review-b": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-b-model",
                    ),
                },
            )
            node = WorkflowNode(
                id="review.node.reviewer.log.failure",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review-a", role=ProviderRole.REVIEWER),
                    ProviderSpec(provider="review-b", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = TimedTaskOutputInvoker(
                outputs_by_task_id={
                    "exec_executor_0": "executor output",
                    "review-a_reviewer_0": review_output(verdict="NO_FINDINGS"),
                },
                delays_by_task_id={"review-a_reviewer_0": 0.01},
            )
            output = FailingLogOutputManager(
                "workflow",
                base_dir=tmp_path,
                failing_provider="review-b",
            )
            events: list[ExecutionEvent] = []

            with self.assertRaisesRegex(RuntimeError, "log setup failed"):
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
            self.assertTrue((node_dir / "review-a_reviewer_0_round1.md").exists())
            failed_events = [
                event
                for event in events
                if event.event_type == "invocation_failed"
                and event.context.provider == "review-b"
            ]
            self.assertEqual(len(failed_events), 1)
            self.assertEqual(failed_events[0].context.task_id, "review-b_reviewer_1")
            self.assertIn("log setup failed", failed_events[0].payload.error or "")
