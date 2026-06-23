import json
import tempfile
import unittest
from pathlib import Path

from crewplane.architecture.contracts import AgentInvoker
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
from crewplane.runtime.execution.common import (
    ExecutionTelemetry,
)
from crewplane.runtime.execution.consensus import (
    extract_verdict,
)
from crewplane.version import SCHEMA_VERSION
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    execute_sequential_stage,
    execute_workflow,
    review_output,
)


class ExecutorReviewLoopFailurePolicyFindingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_executor_invalid_candidate_skips_partial_review_round(
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
                id="review.loop.multi.executor.invalid",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                depth=1,
                audit_rounds=2,
                providers=[
                    ProviderSpec(provider="exec-1", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="exec-2", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "executor one round 1",
                    "   ",
                    "executor one round 1 audit 2",
                    "executor two round 1 audit 2",
                    review_output(verdict="NO_FINDINGS"),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertEqual(len(invoker.calls), 5)
            first_reviewer_index = next(
                index
                for index, call in enumerate(invoker.calls)
                if call["role"] == ProviderRole.REVIEWER
            )
            self.assertEqual(first_reviewer_index, 4)

    async def test_multi_provider_sequential_fatal_policy_raises_on_no_consensus(
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
                id="no.consensus.fatal",
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
                    review_output(
                        major="- Fix the failing edge case",
                        verdict="CHANGES_REQUESTED",
                    ),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            with self.assertRaisesRegex(RuntimeError, "failed to reach consensus"):
                await execute_sequential_stage(config, node, output, invoker=invoker)

    async def test_node_continue_on_failure_overrides_global_fatal_policy(self) -> None:
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
                id="no.consensus.override",
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
            invoker = MockAgentInvoker(
                outputs=[
                    "executor output",
                    review_output(
                        minor="- Add assertions for the new branch",
                        verdict="CHANGES_REQUESTED",
                    ),
                ]
            )
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)
            self.assertEqual(len(invoker.calls), 4)

    async def test_reviewer_invocation_disables_findings_extraction(self) -> None:
        class CapturingInvoker(AgentInvoker):
            def __init__(self) -> None:
                self.contexts = []

            def log_presentation_for(
                self,
                config: AgentConfig,  # noqa: ARG002 - Required by protocol.
            ) -> None:
                return None

            async def invoke(
                self,
                config: AgentConfig,  # noqa: ARG002 - Required by test double or callback signature.
                model: str,  # noqa: ARG002 - Required by test double or callback signature.
                prompt: str,  # noqa: ARG002 - Required by test double or callback signature.
                output_file: Path,
                cwd: Path,  # noqa: ARG002 - Required by test double or callback signature.
                log_file: Path | None = None,  # noqa: ARG002 - Required by test double or callback signature.
                invocation_context=None,  # type: ignore[no-untyped-def]
            ) -> None:
                self.contexts.append(invocation_context)
                assert invocation_context is not None
                if invocation_context.role == ProviderRole.EXECUTOR:
                    output_file.write_text(
                        "\n".join(
                            [
                                "executor output",
                                "",
                                "<!-- findings -->",
                                "- concise finding",
                                "<!-- /findings -->",
                            ]
                        ),
                        encoding="utf-8",
                    )
                    return
                output_file.write_text(
                    review_output(verdict="NO_FINDINGS"),
                    encoding="utf-8",
                )

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
                id="review.node.findings",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                depth=1,
                findings=True,
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = CapturingInvoker()
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            self.assertEqual(len(invoker.contexts), 2)
            assert invoker.contexts[0] is not None
            assert invoker.contexts[1] is not None
            self.assertTrue(invoker.contexts[0].findings_enabled)
            self.assertFalse(invoker.contexts[1].findings_enabled)

    async def test_findings_enabled_multi_executor_sequential_collects_executor_findings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="m1"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="m2"),
                    "review": AgentConfig(cli_cmd=["mock"], default_model="m3"),
                },
            )
            workflow = WorkflowPlan(
                name="multi.executor.findings",
                nodes=[
                    WorkflowNode(
                        id="review.chain",
                        mode="sequential",
                        findings=True,
                        prompt_segments=[
                            PromptSegment(
                                role=PromptSegmentRole.SHARED, content="Review this."
                            )
                        ],
                        providers=[
                            ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR),
                            ProviderSpec(provider="beta", role=ProviderRole.EXECUTOR),
                            ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
                        ],
                    )
                ],
            )
            invoker = MockAgentInvoker(
                outputs=[
                    "\n".join(
                        [
                            "alpha executor output",
                            "",
                            "<!-- findings -->",
                            "- alpha finding",
                            "<!-- /findings -->",
                        ]
                    ),
                    "\n".join(
                        [
                            "beta executor output",
                            "",
                            "<!-- findings -->",
                            "- beta finding",
                            "<!-- /findings -->",
                        ]
                    ),
                    review_output(verdict="NO_FINDINGS"),
                ]
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)

            await execute_workflow(config, workflow, output, invoker=invoker)

            findings_text = output.get_stage_findings_path("review.chain").read_text(
                encoding="utf-8"
            )
            result_text = output.get_stage_output_path("review.chain").read_text(
                encoding="utf-8"
            )

            self.assertIn("## alpha_executor_0", findings_text)
            self.assertIn("## beta_executor_1", findings_text)
            self.assertIn("- alpha finding", findings_text)
            self.assertIn("- beta finding", findings_text)
            self.assertNotIn("## Major Issues", findings_text)
            self.assertNotIn("VERDICT:", findings_text)

            self.assertIn("## alpha_executor_0", result_text)
            self.assertIn("## beta_executor_1", result_text)
            self.assertIn("## review_reviewer_0", result_text)

    async def test_contradictory_reviewer_contract_is_normalized(self) -> None:
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
                id="review.node.invalid",
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
                    review_output(
                        nitpicks="- Rename the summary subsection",
                        verdict="NO_FINDINGS",
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
            reviewer_output = (node_dir / "review_reviewer_0_round1.md").read_text(
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

            self.assertEqual(extract_verdict(reviewer_output), "NITS_ONLY")
            self.assertIn("VERDICT: NO_FINDINGS", raw_output)
            self.assertEqual(metadata["normalized_verdict"], "NITS_ONLY")
            self.assertEqual(metadata["original_verdict"], "NO_FINDINGS")
            warning_events = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_output_normalization"
            ]
            self.assertEqual(len(warning_events), 1)
