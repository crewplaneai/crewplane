import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.core.config import AgentConfig, Config
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.observability.events import ExecutionEvent
from orchestrator_cli.observability.layout import compute_topology_layout
from orchestrator_cli.observability.persistent import PersistentRunLogger
from orchestrator_cli.observability.runtime import ObservabilityHub
from orchestrator_cli.observability.types import RunContext
from orchestrator_cli.runtime.agent.usage import estimate_token_count
from orchestrator_cli.version import SCHEMA_VERSION
from tests.helpers.observability import topology_from_workflow
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    BlockingSnapshotObserver,
    DelayByModelInvoker,
    TimedTaskOutputInvoker,
    execute_workflow,
    review_output,
)


class WorkflowVisibilityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_invocation_events_follow_completion_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "slow": AgentConfig(cli_cmd=["mock"], default_model="slow-model"),
                    "fast": AgentConfig(cli_cmd=["mock"], default_model="fast-model"),
                },
            )
            workflow = WorkflowPlan(
                name="parallel.events",
                nodes=[
                    WorkflowNode(
                        id="node.parallel",
                        mode="parallel",
                        prompt_segments=[PromptSegment(role="shared", content="run")],
                        providers=[
                            ProviderSpec(provider="slow"),
                            ProviderSpec(provider="fast"),
                        ],
                    )
                ],
            )
            invoker = DelayByModelInvoker(
                delays={"slow-model": 0.05, "fast-model": 0.0}
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            await execute_workflow(
                config,
                workflow,
                output,
                invoker=invoker,
                event_sink=events.append,
                run_id=output.run_id,
                suppress_progress_output=True,
            )

            finished = [
                event for event in events if event.event_type == "invocation_finished"
            ]
            self.assertEqual(len(finished), 2)
            self.assertTrue(
                (finished[0].context.task_id or "").startswith("fast_executor_")
            )
            self.assertTrue(
                (finished[1].context.task_id or "").startswith("slow_executor_")
            )
            self.assertEqual(finished[0].payload.attempt_count, 1)
            self.assertEqual(finished[0].payload.output_extraction_status, "success")
            self.assertEqual(finished[0].payload.provider_usage_status, "none")
            self.assertEqual(
                finished[0].payload.visible_estimate_tokens,
                estimate_token_count(len("run"))
                + estimate_token_count(len("done: fast-model")),
            )
            self.assertIsNotNone(finished[1].payload.visible_estimate_tokens)

    async def test_scheduler_and_layout_follow_frontmatter_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha"),
                },
            )
            workflow = WorkflowPlan(
                name="frontmatter-order",
                nodes=[
                    WorkflowNode(
                        id="node.z",
                        mode="sequential",
                        prompt_segments=[PromptSegment(role="shared", content="z")],
                        providers=[ProviderSpec(provider="alpha", role="executor")],
                    ),
                    WorkflowNode(
                        id="node.a",
                        mode="sequential",
                        prompt_segments=[PromptSegment(role="shared", content="a")],
                        providers=[ProviderSpec(provider="alpha", role="executor")],
                    ),
                ],
            )
            invoker = DelayByModelInvoker(delays={"alpha": 0})
            output = OutputManager(workflow.name, base_dir=tmp_path)
            events: list[ExecutionEvent] = []

            await execute_workflow(
                config,
                workflow,
                output,
                invoker=invoker,
                event_sink=events.append,
                run_id=output.run_id,
                suppress_progress_output=True,
            )

            started_nodes = [
                event.context.node_id
                for event in events
                if event.event_type == "node_started"
            ]
            self.assertGreaterEqual(len(started_nodes), 2)
            self.assertEqual(started_nodes[:2], ["node.z", "node.a"])

            layout = compute_topology_layout(topology_from_workflow(workflow))
            self.assertEqual(layout.waves[0], ("node.z", "node.a"))

    async def test_review_loop_drift_guard_allows_queued_event_log_delivery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="exec-model"),
                    "review": AgentConfig(
                        cli_cmd=["mock"], default_model="review-model"
                    ),
                },
            )
            workflow = WorkflowPlan(
                name="review-loop.queued.events",
                nodes=[
                    WorkflowNode(
                        id="review.node",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(role="shared", content="Review this.")
                        ],
                        providers=[
                            ProviderSpec(provider="exec", role="executor"),
                            ProviderSpec(provider="review", role="reviewer"),
                        ],
                    )
                ],
            )
            invoker = TimedTaskOutputInvoker(
                outputs_by_task_id={
                    "exec_executor_0": "review-loop executor output",
                    "review_reviewer_0": review_output(verdict="NO_FINDINGS"),
                },
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)
            persistent_logger = PersistentRunLogger(output)
            blocking_observer = BlockingSnapshotObserver()

            with ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                observers=[blocking_observer, persistent_logger],
                refresh_per_second=0,
            ) as hub:
                self.assertTrue(blocking_observer.entered.wait(timeout=1.0))
                try:
                    await execute_workflow(
                        config,
                        workflow,
                        output,
                        invoker=invoker,
                        event_sink=hub.emit,
                        run_id=output.run_id,
                        suppress_progress_output=True,
                    )
                finally:
                    blocking_observer.release.set()

            event_log = output.get_orchestrator_event_log_path().read_text(
                encoding="utf-8"
            )
            self.assertIn('"event_type": "invocation_started"', event_log)
            self.assertIn('"event_type": "invocation_finished"', event_log)

    async def test_review_loop_ignores_shared_result_writes_from_concurrent_nodes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="exec-model"),
                    "review": AgentConfig(
                        cli_cmd=["mock"], default_model="review-model"
                    ),
                    "aux": AgentConfig(cli_cmd=["mock"], default_model="aux-model"),
                },
            )
            workflow = WorkflowPlan(
                name="review-loop.concurrent.results",
                nodes=[
                    WorkflowNode(
                        id="review.node",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(role="shared", content="Review this.")
                        ],
                        providers=[
                            ProviderSpec(provider="exec", role="executor"),
                            ProviderSpec(provider="review", role="reviewer"),
                        ],
                    ),
                    WorkflowNode(
                        id="aux.node",
                        mode="sequential",
                        prompt_segments=[
                            PromptSegment(role="shared", content="Run aux work.")
                        ],
                        providers=[ProviderSpec(provider="aux", role="executor")],
                    ),
                ],
            )
            invoker = TimedTaskOutputInvoker(
                outputs_by_task_id={
                    "exec_executor_0": "review-loop executor output",
                    "review_reviewer_0": review_output(verdict="NO_FINDINGS"),
                    "aux_executor_0": "auxiliary output",
                },
                delays_by_task_id={"exec_executor_0": 0.05},
            )
            output = OutputManager(workflow.name, base_dir=tmp_path)
            persistent_logger = PersistentRunLogger(output)
            persistent_logger.start(
                RunContext(
                    workflow_topology=topology_from_workflow(workflow),
                    run_id=output.run_id,
                    refresh_per_second=0,
                )
            )
            events: list[ExecutionEvent] = []

            def record_event(event: ExecutionEvent) -> None:
                events.append(event)
                persistent_logger.record_event(event)

            await execute_workflow(
                config,
                workflow,
                output,
                invoker=invoker,
                event_sink=record_event,
                run_id=output.run_id,
                suppress_progress_output=True,
            )

            review_result = output.get_stage_output_path("review.node").read_text(
                encoding="utf-8"
            )
            self.assertIn("review-loop executor output", review_result)
            drift_errors = [
                event
                for event in events
                if event.event_type == "runtime_log"
                and event.payload.operation == "review_loop_artifact_drift"
                and event.payload.level == "error"
            ]
            self.assertEqual(drift_errors, [])
