import tempfile
import unittest
from pathlib import Path

from crewplane.artifacts import OutputManager
from crewplane.core.config import AgentConfig, Config
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.persistent import PersistentRunLogger
from crewplane.observability.types import RunContext
from crewplane.runtime.execution.common import (
    ExecutionTelemetry,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.observability import topology_from_workflow
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    ArtifactDriftInvoker,
    MockAgentInvoker,
    execute_sequential_stage,
    review_output,
)


class ExecutorReviewLoopArtifactDriftTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_provider_sequential_raises_on_result_artifact_drift(
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
                id="review.loop.drift.fatal",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            output = OutputManager("workflow", base_dir=tmp_path)
            stage_result_path = output.get_stage_output_path(node.id)
            invoker = ArtifactDriftInvoker(
                outputs=["executor output round 1"],
                mutations_by_call={
                    0: [(stage_result_path, "tampered result artifact")]
                },
            )

            with self.assertRaisesRegex(RuntimeError, "fatal artifacts"):
                await execute_sequential_stage(config, node, output, invoker=invoker)

    async def test_multi_provider_sequential_raises_on_summary_artifact_drift(
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
                id="review.loop.summary.drift",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            output = OutputManager("workflow", base_dir=tmp_path)
            summary_path = output.get_run_summary_path()
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text("baseline summary", encoding="utf-8")
            invoker = ArtifactDriftInvoker(
                outputs=["executor output round 1"],
                mutations_by_call={0: [(summary_path, "tampered summary")]},
            )

            with self.assertRaisesRegex(RuntimeError, "fatal artifacts"):
                await execute_sequential_stage(config, node, output, invoker=invoker)

    async def test_multi_provider_sequential_raises_on_destructive_event_log_drift(
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
                id="review.loop.event.log.drift",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            output = OutputManager("workflow", base_dir=tmp_path)
            event_log_path = output.get_run_event_log_path()
            event_log_path.parent.mkdir(parents=True, exist_ok=True)
            event_log_path.write_text('{"event":"baseline"}\n', encoding="utf-8")
            invoker = ArtifactDriftInvoker(
                outputs=["executor output round 1"],
                mutations_by_call={0: [(event_log_path, "tampered event log")]},
            )

            with self.assertRaisesRegex(RuntimeError, "fatal artifacts"):
                await execute_sequential_stage(config, node, output, invoker=invoker)

    async def test_multi_provider_sequential_raises_on_event_log_append_drift(
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
                id="review.loop.event.log.append.drift",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
            )
            output = OutputManager("workflow", base_dir=tmp_path)
            event_log_path = output.get_run_event_log_path()
            event_log_path.parent.mkdir(parents=True, exist_ok=True)
            event_log_path.write_text('{"event":"baseline"}\n', encoding="utf-8")
            invoker = ArtifactDriftInvoker(
                outputs=["executor output round 1"],
                append_mutations_by_call={0: [(event_log_path, "tampered append\n")]},
            )

            with self.assertRaisesRegex(RuntimeError, "fatal artifacts"):
                await execute_sequential_stage(config, node, output, invoker=invoker)

    async def test_parallel_reviewer_event_log_creation_drift_is_fatal(
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
                id="review.loop.parallel.event.log.creation",
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
            event_log_path = output.get_run_event_log_path()
            invoker = ArtifactDriftInvoker(
                outputs=[
                    "executor output round 1",
                    review_output(verdict="NO_FINDINGS"),
                    review_output(verdict="NO_FINDINGS"),
                ],
                append_mutations_by_call={1: [(event_log_path, "tampered append\n")]},
            )

            with self.assertRaisesRegex(RuntimeError, "fatal artifacts"):
                await execute_sequential_stage(config, node, output, invoker=invoker)

    async def test_multi_provider_sequential_allows_runtime_event_log_appends(
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
                id="review.loop.runtime.event.log",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="Review this.")],
                depth=1,
                providers=[
                    ProviderSpec(provider="exec", role="executor"),
                    ProviderSpec(provider="review", role="reviewer"),
                ],
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
            invoker = MockAgentInvoker(
                outputs=[
                    "executor output round 1",
                    review_output(verdict="NO_FINDINGS"),
                ]
            )

            await execute_sequential_stage(
                config,
                node,
                output,
                invoker=invoker,
                telemetry=ExecutionTelemetry(
                    workflow_name="workflow",
                    run_id=output.run_id,
                    event_sink=persistent_logger.record_event,
                ),
            )

            event_log = output.get_run_event_log_path().read_text(encoding="utf-8")
            self.assertIn('"event_type": "invocation_started"', event_log)
            self.assertIn('"event_type": "invocation_finished"', event_log)
