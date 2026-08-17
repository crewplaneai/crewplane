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
from crewplane.runtime.execution.errors import NodeExecutionError
from crewplane.runtime.execution.fragment_assembler import ResolvedPrompt
from crewplane.runtime.execution.provider_call import (
    generated_files as provider_generated_files,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.observability import topology_from_workflow
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    FailingLogOutputManager,
    MockAgentInvoker,
    ParallelReviewerBarrierInvoker,
    TaskOutputInvoker,
    execute_sequential_stage,
    review_loop_status_path,
    review_output,
)


class ParallelReviewerStreamingLogInvoker(ParallelReviewerBarrierInvoker):
    def __init__(self) -> None:
        super().__init__()
        self.slow_log_ready = asyncio.Event()
        self.release_slow_reviewer = asyncio.Event()
        self.slow_log_path: Path | None = None

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by test double signature.
        model: str,  # noqa: ARG002 - Required by test double signature.
        prompt: str,  # noqa: ARG002 - Required by test double signature.
        output_file: Path,
        cwd: Path,  # noqa: ARG002 - Required by test double signature.
        log_file: Path | None = None,
        invocation_context=None,  # type: ignore[no-untyped-def]
    ) -> None:
        assert invocation_context is not None
        if invocation_context.role == ProviderRole.EXECUTOR:
            output_file.write_text("executor output", encoding="utf-8")
            return
        if invocation_context.task_id == "review-b_reviewer_1":
            assert log_file is not None
            self.slow_log_path = log_file
            log_file.write_text("review-b streaming", encoding="utf-8")
            self.slow_log_ready.set()
            await asyncio.wait_for(self.release_slow_reviewer.wait(), timeout=1.0)
        else:
            await asyncio.wait_for(self.slow_log_ready.wait(), timeout=1.0)
            asyncio.get_running_loop().call_later(
                0.05,
                self.release_slow_reviewer.set,
            )
        output_file.write_text(review_output(verdict="NO_FINDINGS"), encoding="utf-8")


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

            node_dir = output.get_node_dir(node_artifact_request(node.id))
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

    async def test_reviewer_first_runs_round_zero_review_then_canonical_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "README.md").write_text(
                "buggy implementation context", encoding="utf-8"
            )
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.node.reviewer.first",
                mode="sequential",
                review_starts_with="reviewer",
                prompt_segments=[
                    PromptSegment(
                        role=PromptSegmentRole.SHARED,
                        content="Review {{file:README.md}} and fix issues.",
                    )
                ],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    review_output(verdict="NO_FINDINGS"),
                    "canonical executor output",
                    review_output(verdict="NO_FINDINGS"),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertEqual(
                [(call["role"], call["round_num"]) for call in invoker.calls],
                [
                    (ProviderRole.REVIEWER, 0),
                    (ProviderRole.EXECUTOR, 1),
                    (ProviderRole.REVIEWER, 1),
                ],
            )
            pre_review_prompt = str(invoker.calls[0]["prompt"])
            self.assertIn("Existing review context:", pre_review_prompt)
            self.assertIn("buggy implementation context", pre_review_prompt)
            self.assertNotIn("Current executor output(s):", pre_review_prompt)
            self.assertNotIn(
                "Review only the current executor output(s)",
                pre_review_prompt,
            )

            executor_prompt = str(invoker.calls[1]["prompt"])
            self.assertIn("Initial reviewer handoff:", executor_prompt)
            self.assertIn("found no unresolved major or minor issues", executor_prompt)
            self.assertNotIn("Previous unresolved review state:", executor_prompt)

            reviewer_prompt = str(invoker.calls[2]["prompt"])
            self.assertIn("Current executor output(s):", reviewer_prompt)
            self.assertIn("canonical executor output", reviewer_prompt)

            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")
            self.assertTrue((node_dir / "review_reviewer_0_round0.md").exists())
            self.assertTrue((node_dir / "review_reviewer_0_round0.raw.txt").exists())
            self.assertTrue(
                (node_dir / "review_reviewer_0_round0.review.json").exists()
            )
            self.assertTrue(
                (
                    node_dir
                    / "review-state"
                    / f"{safe_artifact_name('review_reviewer_0')}-round-0.state.json"
                ).exists()
            )

            status_payload = json.loads(
                review_loop_status_path(node_dir).read_text(encoding="utf-8")
            )
            self.assertNotIn("round0", json.dumps(status_payload))
            self.assertEqual(
                [
                    entry["path"]
                    for entry in status_payload["canonical_executor_outputs"]
                ],
                ["exec_executor_0_round1.md"],
            )
            self.assertEqual(
                [entry["path"] for entry in status_payload["reviewer_outputs"]],
                ["review_reviewer_0_round1.md"],
            )

    async def test_reviewer_first_blocking_feedback_reaches_executor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "README.md").write_text("review context", encoding="utf-8")
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                },
            )
            node = WorkflowNode(
                id="review.node.reviewer.first.blocking",
                mode="sequential",
                review_starts_with="reviewer",
                prompt_segments=[
                    PromptSegment(
                        role=PromptSegmentRole.SHARED,
                        content="Review {{file:README.md}} and fix issues.",
                    )
                ],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    review_output(
                        verdict="CHANGES_REQUESTED",
                        major="- Fix the retry branch",
                    ),
                    "canonical fixed output",
                    review_output(verdict="NO_FINDINGS"),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            executor_prompt = str(invoker.calls[1]["prompt"])
            self.assertIn("Initial reviewer handoff:", executor_prompt)
            self.assertIn("## Reviewer Feedback", executor_prompt)
            self.assertIn("Fix the retry branch", executor_prompt)
            self.assertNotIn("Previous unresolved review state:", executor_prompt)

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
                    NodeExecutionError,
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
                    NodeExecutionError,
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

            node_dir = output.get_node_dir(node_artifact_request(node.id))
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
            node_dir = output.get_node_dir(node_artifact_request(node.id))
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
            invoker = ParallelReviewerBarrierInvoker()
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertEqual(
                invoker.started_reviewer_task_ids,
                {"review-a_reviewer_0", "review-b_reviewer_1"},
            )

    async def test_parallel_reviewer_peer_log_is_not_read_by_drift_snapshot(
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
                id="review.node.parallel.logs",
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
            invoker = ParallelReviewerStreamingLogInvoker()
            output = OutputManager(
                "workflow",
                base_dir=tmp_path,
                log_cli_output=True,
            )
            original_read_bytes = Path.read_bytes
            peer_log_read_count = 0

            def append_after_peer_log_read(path: Path) -> bytes:
                nonlocal peer_log_read_count
                payload = original_read_bytes(path)
                if path == invoker.slow_log_path:
                    peer_log_read_count += 1
                    with path.open("ab") as stream:
                        stream.write(b" still running")
                return payload

            with patch.object(Path, "read_bytes", new=append_after_peer_log_read):
                await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertIsNotNone(invoker.slow_log_path)
            self.assertEqual(peer_log_read_count, 0)

    async def test_parallel_reviewer_private_snapshots_do_not_overlap(
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
            invoker = TaskOutputInvoker(
                outputs_by_task_id={
                    "exec_executor_0": "executor output",
                    "review-a_reviewer_0": review_output(verdict="NO_FINDINGS"),
                    "review-b_reviewer_1": review_output(verdict="NO_FINDINGS"),
                },
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

            snapshot_output_paths: list[Path] = []
            snapshot_roots: list[Path] = []
            original_snapshot = (
                provider_generated_files.snapshot_generated_file_workspace
            )

            def coordinate_snapshot(*args, **kwargs):  # type: ignore[no-untyped-def]
                snapshot_root = original_snapshot(*args, **kwargs)
                snapshot_output_paths.append(args[0])
                snapshot_roots.append(snapshot_root)
                return snapshot_root

            with patch.object(
                provider_generated_files,
                "snapshot_generated_file_workspace",
                side_effect=coordinate_snapshot,
            ):
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

            reviewer_snapshot_paths = [
                path
                for path in snapshot_output_paths
                if path.name == "provider-output.md"
            ]
            self.assertEqual(len(reviewer_snapshot_paths), 2)
            self.assertNotEqual(
                reviewer_snapshot_paths[0].parent,
                reviewer_snapshot_paths[1].parent,
            )
            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")
            reviewer_snapshot_roots = [
                root
                for source, root in zip(
                    snapshot_output_paths, snapshot_roots, strict=True
                )
                if source.name == "provider-output.md"
            ]
            self.assertTrue(all(root.exists() for root in reviewer_snapshot_roots))
            self.assertEqual(
                set(reviewer_snapshot_roots),
                {
                    provider_generated_files.generated_file_source_root(
                        node_dir / "review-a_reviewer_0_round1.md"
                    ),
                    provider_generated_files.generated_file_source_root(
                        node_dir / "review-b_reviewer_1_round1.md"
                    ),
                },
            )
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
            release_review_a = asyncio.Event()
            invoker = TaskOutputInvoker(
                outputs_by_task_id={
                    "exec_executor_0": "executor output",
                    "review-a_reviewer_0": review_output(verdict="NO_FINDINGS"),
                },
                gates_by_task_id={"review-a_reviewer_0": release_review_a},
            )

            def release_in_flight_reviewer() -> None:
                self.assertIn(
                    "review-a",
                    output.log_setup_providers,
                    "review-a lifecycle had not started when review-b setup failed",
                )
                release_review_a.set()

            output = FailingLogOutputManager(
                "workflow",
                base_dir=tmp_path,
                failing_provider="review-b",
                on_failure=release_in_flight_reviewer,
            )
            events: list[ExecutionEvent] = []

            try:
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
            finally:
                release_review_a.set()

            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")
            self.assertFalse((node_dir / "review-a_reviewer_0_round1.md").exists())
            failed_events = [
                event
                for event in events
                if event.event_type == "invocation_failed"
                and event.context.provider == "review-b"
            ]
            self.assertEqual(len(failed_events), 1)
            self.assertEqual(failed_events[0].context.task_id, "review-b_reviewer_1")
            self.assertIn("log setup failed", failed_events[0].payload.error or "")
