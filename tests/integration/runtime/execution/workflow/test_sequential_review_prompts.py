import json
import tempfile
import unittest
from pathlib import Path

from crewplane.artifacts import OutputManager, safe_artifact_name
from crewplane.core.config import AgentConfig, Config
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    execute_sequential_stage,
    review_loop_status_path,
    review_output,
)


class ExecutorSequentialStageBasicsTests(unittest.IsolatedAsyncioTestCase):
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
            assert len(invoker.calls) == 2

            reviewer_prompt = invoker.calls[1]["prompt"]
            assert reviewer_prompt.startswith("You are acting only as a reviewer.")
            assert "Do not modify files" in reviewer_prompt
            assert "Node artifacts directory:" not in reviewer_prompt
            assert reviewer_prompt.index(
                "Task context:\nReview this."
            ) < reviewer_prompt.index("Current executor output(s):")
            assert reviewer_prompt.index(
                "VERDICT: CHANGES_REQUESTED | NITS_ONLY | NO_FINDINGS"
            ) > reviewer_prompt.index("Current executor output(s):")
            assert "## Major Issues" in reviewer_prompt
            assert "## Minor Issues" in reviewer_prompt
            assert "## Nitpicks" in reviewer_prompt

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

            assert [(call["role"], call["round_num"]) for call in invoker.calls] == [
                (ProviderRole.REVIEWER, 0),
                (ProviderRole.EXECUTOR, 1),
                (ProviderRole.REVIEWER, 1),
            ]
            pre_review_prompt = str(invoker.calls[0]["prompt"])
            assert "Existing review context:" in pre_review_prompt
            assert "buggy implementation context" in pre_review_prompt
            assert "Current executor output(s):" not in pre_review_prompt
            assert "Review only the current executor output(s)" not in pre_review_prompt

            executor_prompt = str(invoker.calls[1]["prompt"])
            assert "Initial reviewer handoff:" in executor_prompt
            assert "found no unresolved major or minor issues" in executor_prompt
            assert "Previous unresolved review state:" not in executor_prompt

            reviewer_prompt = str(invoker.calls[2]["prompt"])
            assert "Current executor output(s):" in reviewer_prompt
            assert "canonical executor output" in reviewer_prompt

            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")
            assert (node_dir / "review_reviewer_0_round0.md").exists()
            assert (node_dir / "review_reviewer_0_round0.raw.txt").exists()
            assert (node_dir / "review_reviewer_0_round0.review.json").exists()
            assert (
                node_dir
                / "review-state"
                / f"{safe_artifact_name('review_reviewer_0')}-round-0.state.json"
            ).exists()

            status_payload = json.loads(
                review_loop_status_path(node_dir).read_text(encoding="utf-8")
            )
            assert "round0" not in json.dumps(status_payload)
            assert [
                entry["path"] for entry in status_payload["canonical_executor_outputs"]
            ] == ["exec_executor_0_round1.md"]
            assert [entry["path"] for entry in status_payload["reviewer_outputs"]] == [
                "review_reviewer_0_round1.md"
            ]

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
            assert "Initial reviewer handoff:" in executor_prompt
            assert "## Reviewer Feedback" in executor_prompt
            assert "Fix the retry branch" in executor_prompt
            assert "Previous unresolved review state:" not in executor_prompt

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

            assert len(invoker.calls) == 2
            executor_prompt = str(invoker.calls[0]["prompt"])
            reviewer_prompt = str(invoker.calls[1]["prompt"])

            assert "Shared context." in executor_prompt
            assert "Executor delta." in executor_prompt
            assert "Reviewer delta." not in executor_prompt

            assert "Task context:\nShared context.\nReviewer delta." in reviewer_prompt
            assert "Executor delta." not in reviewer_prompt

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

            assert len(invoker.calls) == 3
            assert invoker.calls[0]["prompt"] == invoker.calls[1]["prompt"]

            reviewer_prompt = invoker.calls[2]["prompt"]
            assert "=== exec-1 executor output ===" in reviewer_prompt
            assert "=== exec-2 executor output ===" in reviewer_prompt
            assert reviewer_prompt.index(
                "=== exec-1 executor output ==="
            ) < reviewer_prompt.index("=== exec-2 executor output ===")
            assert "executor one output" in reviewer_prompt
            assert "executor two output" in reviewer_prompt

            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")
            assert str(node_dir / "exec-1_executor_0_round1.md") in reviewer_prompt
            assert str(node_dir / "exec-2_executor_1_round1.md") in reviewer_prompt

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

            assert len(invoker.calls) == 4
            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")
            assert not (node_dir / "exec_executor_0_round3.md").exists()
            assert not (node_dir / "review_reviewer_0_round3.md").exists()
            second_round_reviewer_prompt = invoker.calls[3]["prompt"]
            assert "Previous unresolved review state:" in second_round_reviewer_prompt
            assert "Add missing regression tests" in second_round_reviewer_prompt
