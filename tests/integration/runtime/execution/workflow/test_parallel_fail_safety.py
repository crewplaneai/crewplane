import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crewplane.artifacts import OutputManager, safe_artifact_name
from crewplane.core.config import AgentConfig, Config, Settings
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
from crewplane.version import SCHEMA_VERSION
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    DelayByModelInvoker,
    FailingLogOutputManager,
    FindingsSelectiveFailInvoker,
    MockAgentInvoker,
    SelectiveFailInvoker,
    execute_parallel_stage,
    execute_workflow,
)


class ExecutorParallelFailSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_node_fails_when_resolved_executor_prompt_is_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={"alpha": AgentConfig(cli_cmd=["mock"], default_model="ok")},
            )
            node = WorkflowNode(
                id="parallel.empty.prompt",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(
                        role="shared", content="{{env:EMPTY_PARALLEL_PROMPT}}"
                    )
                ],
                providers=[ProviderSpec(provider="alpha")],
            )
            invoker = MockAgentInvoker(outputs=["unused"])
            output = OutputManager("workflow", base_dir=tmp_path)

            with (
                patch.dict("os.environ", {"EMPTY_PARALLEL_PROMPT": ""}, clear=False),
                self.assertRaisesRegex(
                    RuntimeError,
                    "Resolved executor prompt for node 'parallel.empty.prompt' is empty after fragment assembly.",
                ),
            ):
                await execute_parallel_stage(config, node, output, invoker=invoker)

            self.assertEqual(invoker.calls, [])

    async def test_parallel_node_raises_on_failure_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="fail"),
                },
            )
            node = WorkflowNode(
                id="parallel.review",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="run")],
                providers=[
                    ProviderSpec(provider="alpha"),
                    ProviderSpec(provider="beta"),
                ],
            )
            invoker = SelectiveFailInvoker(failing_models={"fail"})
            output = OutputManager("workflow", base_dir=tmp_path)

            with self.assertRaisesRegex(RuntimeError, "exceeded failure threshold"):
                await execute_parallel_stage(config, node, output, invoker=invoker)

            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            failed_file = (
                node_dir / f"{safe_artifact_name('beta')}_executor_1_round1.md"
            )
            self.assertTrue(failed_file.exists())
            self.assertIn("Invocation Failed", failed_file.read_text(encoding="utf-8"))

    async def test_parallel_node_allows_partial_failures_with_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="fail"),
                    "gamma": AgentConfig(cli_cmd=["mock"], default_model="ok-2"),
                },
            )
            node = WorkflowNode(
                id="parallel.threshold",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="run")],
                failure_threshold=1,
                providers=[
                    ProviderSpec(provider="alpha"),
                    ProviderSpec(provider="beta"),
                    ProviderSpec(provider="gamma"),
                ],
            )
            invoker = SelectiveFailInvoker(failing_models={"fail"})
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_parallel_stage(config, node, output, invoker=invoker)

            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            success_file = (
                node_dir / f"{safe_artifact_name('alpha')}_executor_0_round1.md"
            )
            self.assertTrue(success_file.exists())
            self.assertIn("success: ok", success_file.read_text(encoding="utf-8"))

    async def test_findings_enabled_parallel_node_survives_allowed_failures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="fail"),
                    "gamma": AgentConfig(cli_cmd=["mock"], default_model="ok-2"),
                },
            )
            workflow = WorkflowPlan(
                name="parallel.findings.threshold",
                nodes=[
                    WorkflowNode(
                        id="parallel.threshold",
                        mode="parallel",
                        findings=True,
                        prompt_segments=[PromptSegment(role="shared", content="run")],
                        failure_threshold=1,
                        providers=[
                            ProviderSpec(provider="alpha"),
                            ProviderSpec(provider="beta"),
                            ProviderSpec(provider="gamma"),
                        ],
                    )
                ],
            )
            invoker = FindingsSelectiveFailInvoker(failing_models={"fail"})
            output = OutputManager(workflow.name, base_dir=tmp_path)

            await execute_workflow(config, workflow, output, invoker=invoker)

            findings_file = output.get_stage_findings_path("parallel.threshold")
            result_file = output.get_stage_output_path("parallel.threshold")
            self.assertTrue(findings_file.exists())
            findings_text = findings_file.read_text(encoding="utf-8")
            self.assertIn("concise finding: ok", findings_text)
            self.assertIn("concise finding: ok-2", findings_text)
            self.assertIn("beta_executor_1", findings_text)
            self.assertIn("No findings were produced", findings_text)
            self.assertIn(
                "Invocation Failed",
                result_file.read_text(encoding="utf-8"),
            )

    async def test_findings_enabled_parallel_node_writes_failure_findings_when_all_continue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="fail-a"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="fail-b"),
                    "summary": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                },
            )
            workflow = WorkflowPlan(
                name="parallel.findings.all.failed",
                nodes=[
                    WorkflowNode(
                        id="parallel.review",
                        mode="parallel",
                        findings=True,
                        continue_on_failure=True,
                        prompt_segments=[PromptSegment(role="shared", content="run")],
                        providers=[
                            ProviderSpec(provider="alpha"),
                            ProviderSpec(provider="beta"),
                        ],
                    ),
                    WorkflowNode(
                        id="summary",
                        mode="sequential",
                        needs=["parallel.review"],
                        prompt_segments=[
                            PromptSegment(
                                role="shared",
                                content="Summarize {{parallel.review.findings}}",
                            )
                        ],
                        providers=[ProviderSpec(provider="summary", role="executor")],
                    ),
                ],
            )
            invoker = SelectiveFailInvoker(failing_models={"fail-a", "fail-b"})
            output = OutputManager(workflow.name, base_dir=tmp_path)

            await execute_workflow(config, workflow, output, invoker=invoker)

            findings_file = output.get_stage_findings_path("parallel.review")
            self.assertTrue(findings_file.exists())
            findings_text = findings_file.read_text(encoding="utf-8")
            self.assertIn("alpha_executor_0", findings_text)
            self.assertIn("beta_executor_1", findings_text)
            self.assertIn("No findings were produced", findings_text)
            self.assertIn("No findings were produced", invoker.calls[-1]["prompt"])

    async def test_parallel_node_emits_failure_events_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="fail"),
                },
            )
            node = WorkflowNode(
                id="parallel.events",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="run")],
                failure_threshold=1,
                providers=[
                    ProviderSpec(provider="alpha"),
                    ProviderSpec(provider="beta"),
                ],
            )
            invoker = SelectiveFailInvoker(failing_models={"fail"})
            output = OutputManager("workflow", base_dir=tmp_path, log_cli_output=True)

            events = []
            telemetry = ExecutionTelemetry(
                workflow_name="workflow",
                run_id="test-run",
                event_sink=lambda e: events.append(e),
                suppress_console_output=True,
            )

            await execute_parallel_stage(
                config, node, output, invoker=invoker, telemetry=telemetry
            )

            # Check for failure events
            failed_events = [e for e in events if e.event_type == "invocation_failed"]
            finished_events = [
                e for e in events if e.event_type == "invocation_finished"
            ]

            self.assertEqual(len(failed_events), 1)
            self.assertEqual(len(finished_events), 1)

            beta_failed = failed_events[0]
            self.assertEqual(beta_failed.context.node_id, "parallel.events")
            self.assertEqual(beta_failed.context.provider, "beta")
            self.assertEqual(
                beta_failed.context.task_id,
                f"{safe_artifact_name('beta')}_executor_1",
            )
            self.assertIsNotNone(beta_failed.context.log_file)
            self.assertGreaterEqual(beta_failed.payload.duration_ms, 0)
            self.assertIn("simulated failure for fail", beta_failed.payload.error or "")

            alpha_finished = finished_events[0]
            self.assertEqual(alpha_finished.context.node_id, "parallel.events")
            self.assertEqual(alpha_finished.context.provider, "alpha")
            self.assertEqual(
                alpha_finished.context.task_id,
                f"{safe_artifact_name('alpha')}_executor_0",
            )

    async def test_parallel_failure_event_sink_error_preserves_original_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="fail"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                },
            )
            node = WorkflowNode(
                id="parallel.failure.event.failure",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="run")],
                failure_threshold=1,
                providers=[
                    ProviderSpec(provider="alpha"),
                    ProviderSpec(provider="beta"),
                ],
            )
            invoker = SelectiveFailInvoker(failing_models={"fail"})
            output = OutputManager("workflow", base_dir=tmp_path, log_cli_output=True)

            def event_sink(event: ExecutionEvent) -> None:
                if event.event_type == "invocation_failed":
                    raise RuntimeError("failed-event sink boom")

            telemetry = ExecutionTelemetry(
                workflow_name="workflow",
                run_id="test-run",
                event_sink=event_sink,
                suppress_console_output=True,
            )

            await execute_parallel_stage(
                config,
                node,
                output,
                invoker=invoker,
                telemetry=telemetry,
            )

            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            failed_file = (
                node_dir / f"{safe_artifact_name('alpha')}_executor_0_round1.md"
            )
            failed_text = failed_file.read_text(encoding="utf-8")
            self.assertIn("Invocation Failed", failed_text)
            self.assertIn("simulated failure for fail", failed_text)
            self.assertNotIn("failed-event sink boom", failed_text)

    async def test_parallel_log_setup_failure_is_captured_by_failure_policy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="ok-2"),
                },
            )
            node = WorkflowNode(
                id="parallel.setup.failure",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="run")],
                failure_threshold=1,
                providers=[
                    ProviderSpec(provider="alpha"),
                    ProviderSpec(provider="beta"),
                ],
            )
            invoker = SelectiveFailInvoker(failing_models=set())
            output = FailingLogOutputManager(
                "workflow",
                base_dir=tmp_path,
                failing_provider="beta",
            )

            await execute_parallel_stage(config, node, output, invoker=invoker)

            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            failed_file = (
                node_dir / f"{safe_artifact_name('beta')}_executor_1_round1.md"
            )
            success_file = (
                node_dir / f"{safe_artifact_name('alpha')}_executor_0_round1.md"
            )
            self.assertTrue(failed_file.exists())
            self.assertTrue(success_file.exists())
            failed_text = failed_file.read_text(encoding="utf-8")
            self.assertIn("Invocation Failed", failed_text)
            self.assertIn("log setup failed", failed_text)

    async def test_parallel_started_event_failure_is_captured_by_failure_policy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="ok"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="ok-2"),
                },
            )
            node = WorkflowNode(
                id="parallel.event.failure",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="run")],
                failure_threshold=1,
                providers=[
                    ProviderSpec(provider="alpha"),
                    ProviderSpec(provider="beta"),
                ],
            )
            invoker = SelectiveFailInvoker(failing_models=set())
            output = OutputManager("workflow", base_dir=tmp_path, log_cli_output=True)
            events: list[ExecutionEvent] = []

            def event_sink(event: ExecutionEvent) -> None:
                if (
                    event.event_type == "invocation_started"
                    and event.context.provider == "beta"
                ):
                    raise RuntimeError("event sink boom")
                events.append(event)

            telemetry = ExecutionTelemetry(
                workflow_name="workflow",
                run_id="test-run",
                event_sink=event_sink,
                suppress_console_output=True,
            )

            await execute_parallel_stage(
                config,
                node,
                output,
                invoker=invoker,
                telemetry=telemetry,
            )

            node_dir = output.get_stage_dir(node.id)
            if node_dir is None:
                self.fail("Expected node directory to be created")
            failed_file = (
                node_dir / f"{safe_artifact_name('beta')}_executor_1_round1.md"
            )
            failed_text = failed_file.read_text(encoding="utf-8")
            self.assertIn("Invocation Failed", failed_text)
            self.assertIn("event sink boom", failed_text)
            failed_events = [
                event
                for event in events
                if event.event_type == "invocation_failed"
                and event.context.provider == "beta"
            ]
            self.assertEqual(len(failed_events), 1)
            self.assertIn("event sink boom", failed_events[0].payload.error or "")

    async def test_parallel_max_invocations_queues_before_started_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                settings=Settings(max_parallel_invocations=1),
                agents={
                    "alpha": AgentConfig(cli_cmd=["mock"], default_model="slow"),
                    "beta": AgentConfig(cli_cmd=["mock"], default_model="fast"),
                },
            )
            node = WorkflowNode(
                id="parallel.queue",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="run")],
                providers=[
                    ProviderSpec(provider="alpha"),
                    ProviderSpec(provider="beta"),
                ],
            )
            invoker = DelayByModelInvoker(delays={"slow": 0.05, "fast": 0.0})
            output = OutputManager("workflow", base_dir=tmp_path)
            events: list[ExecutionEvent] = []
            telemetry = ExecutionTelemetry(
                workflow_name="workflow",
                run_id="test-run",
                event_sink=events.append,
                suppress_console_output=True,
            )

            await execute_parallel_stage(
                config,
                node,
                output,
                invoker=invoker,
                telemetry=telemetry,
            )

            invocation_events = [
                event
                for event in events
                if event.event_type in {"invocation_started", "invocation_finished"}
            ]
            self.assertEqual(
                [
                    (event.event_type, event.context.provider)
                    for event in invocation_events
                ],
                [
                    ("invocation_started", "alpha"),
                    ("invocation_finished", "alpha"),
                    ("invocation_started", "beta"),
                    ("invocation_finished", "beta"),
                ],
            )
            alpha_finished = invocation_events[1]
            beta_started = invocation_events[2]
            beta_finished = invocation_events[3]
            self.assertGreaterEqual(beta_started.timestamp, alpha_finished.timestamp)
            self.assertIsNotNone(alpha_finished.payload.duration_ms)
            self.assertIsNotNone(beta_finished.payload.duration_ms)
            self.assertLess(
                beta_finished.payload.duration_ms or 0,
                alpha_finished.payload.duration_ms or 0,
            )
