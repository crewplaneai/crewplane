import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.observability import PersistentRunLogger
from orchestrator_cli.observability.events import (
    apply_event,
    build_initial_state,
)
from orchestrator_cli.observability.persistent import render_run_summary_terminal
from orchestrator_cli.observability.run_summary.accumulator import (
    MAX_RETAINED_INVOCATION_USAGE_DETAILS,
)
from orchestrator_cli.observability.run_summary.logger import (
    MAX_RETAINED_SUMMARY_EVENTS,
)
from orchestrator_cli.observability.runtime import ObservabilityHub
from orchestrator_cli.observability.types import (
    RunContext,
    RunResult,
)
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)
from tests.integration.observability.runtime.observability_runtime_helpers import (
    RecordingObserver,
    single_node_workflow,
)


class PersistentRunLoggerSummaryTests(unittest.TestCase):
    def test_runtime_log_warning_updates_recent_events(self) -> None:
        workflow = single_node_workflow()
        state = build_initial_state(topology_from_workflow(workflow), run_id="run-1")

        apply_event(
            state,
            make_execution_event(
                event_type="runtime_log",
                workflow_name=workflow.name,
                run_id="run-1",
                node_id="node.a",
                level="warning",
                message="used stderr as output",
                operation="stderr_fallback",
            ),
        )

        self.assertIn(
            "WARN used stderr as output", list(state.nodes["node.a"].recent_events)
        )

    def test_persistent_run_logger_writes_ndjson_and_summary(self) -> None:
        workflow = single_node_workflow()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output = OutputManager(
                workflow.name, base_dir=tmp_path, log_cli_output=True
            )
            stage_dir = output.create_stage_dir("node.a")
            output_file = stage_dir / "alpha_executor_0_round1.md"
            log_file = output.get_log_file(
                stage_name="node.a",
                provider="alpha",
                task_id="alpha_executor_0",
                round_num=1,
            )
            assert log_file is not None
            persistent_logger = PersistentRunLogger(output)
            observer = RecordingObserver()

            with ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                observers=[observer, persistent_logger],
                refresh_per_second=0,
            ) as hub:
                hub.emit(
                    make_execution_event(
                        event_type="workflow_started",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="node_started",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="invocation_started",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                        provider="alpha",
                        role="executor",
                        model="alpha-model",
                        task_id="alpha_executor_0",
                        round_num=1,
                        output_file=str(output_file),
                        log_file=str(log_file),
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="runtime_log",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                        task_id="alpha_executor_0",
                        level="warning",
                        message="stdout was empty; used stderr as output",
                        operation="stderr_fallback",
                        output_file=str(output_file),
                        log_file=str(log_file),
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="invocation_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                        provider="alpha",
                        role="executor",
                        model="alpha-model",
                        task_id="alpha_executor_0",
                        round_num=1,
                        duration_ms=250,
                        attempt_count=2,
                        cli_captured=True,
                        output_extraction_status="success",
                        provider_usage_status="full",
                        provider_tokens={
                            "input": 90,
                            "cached_input": None,
                            "cache_write": None,
                            "output": 12,
                            "reasoning": None,
                            "total": None,
                        },
                        visible_estimate_tokens=42,
                        visible_estimate_method="char-count-lower-bound",
                        visible_estimate_is_lower_bound=True,
                        configured_cost_usd=0.000207,
                        invocation_cost_confidence="full",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="node_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="workflow_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                    )
                )

            event_log = output.get_orchestrator_event_log_path()
            summary_log = output.get_orchestrator_summary_path()
            self.assertTrue(event_log.exists())
            self.assertTrue(summary_log.exists())

            event_log_text = event_log.read_text(encoding="utf-8")
            summary_text = summary_log.read_text(encoding="utf-8")
            self.assertIn('"event_type": "runtime_log"', event_log_text)
            self.assertIn('"operation": "stderr_fallback"', event_log_text)
            self.assertIn(
                "Invocation succeeded with empty stdout; used stderr as output.",
                summary_text,
            )
            self.assertIn(
                "Provider log contains the original stderr lines",
                summary_text,
            )
            self.assertIn('"attempt_count": 2', event_log_text)
            self.assertIn(
                '"provider_tokens": {"cache_write": null, "cached_input": null, "input": 90',
                event_log_text,
            )
            self.assertIn("## Spend Observability", summary_text)
            self.assertIn("provider report: full", summary_text)
            self.assertIn(
                "Configured cost estimate: $0.000207 (confidence: full)", summary_text
            )
            self.assertIn("## Node Outcomes", summary_text)
            self.assertIn("## Artifact References", summary_text)
            last_summary = persistent_logger.last_summary
            self.assertIsNotNone(last_summary)
            assert last_summary is not None
            terminal_summary = render_run_summary_terminal(last_summary)
            self.assertIn(
                "Provider token reports: 1/1 full, 0/1 partial, 0/1 malformed",
                terminal_summary,
            )
            self.assertIn("alpha: 1 invocation(s)", terminal_summary)
            self.assertIn(
                "Configured cost estimate: $0.000207 (full)", terminal_summary
            )

    def test_persistent_run_logger_bounds_retained_event_details(self) -> None:
        workflow = single_node_workflow()
        overflow_count = 5
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
            persistent_logger = PersistentRunLogger(output)

            with ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                observers=[persistent_logger],
                refresh_per_second=0,
            ) as hub:
                for index in range(MAX_RETAINED_SUMMARY_EVENTS + overflow_count):
                    hub.emit(
                        make_execution_event(
                            event_type="runtime_log",
                            workflow_name=workflow.name,
                            run_id=output.run_id,
                            level="warning",
                            message=f"summary warning {index}",
                            operation="summary_retention_test",
                        )
                    )

            self.assertEqual(
                persistent_logger.retained_event_count,
                MAX_RETAINED_SUMMARY_EVENTS,
            )
            self.assertEqual(persistent_logger.dropped_event_count, overflow_count)

            event_log_lines = (
                output.get_orchestrator_event_log_path()
                .read_text(encoding="utf-8")
                .splitlines()
            )
            self.assertEqual(
                len(event_log_lines),
                MAX_RETAINED_SUMMARY_EVENTS + overflow_count,
            )
            summary_text = output.get_orchestrator_summary_path().read_text(
                encoding="utf-8"
            )
            self.assertIn("were omitted from in-memory summary detail", summary_text)
            self.assertIn(
                f"summary warning {MAX_RETAINED_SUMMARY_EVENTS + overflow_count - 1}",
                summary_text,
            )

    def test_summary_rollups_survive_retained_event_detail_cap(self) -> None:
        workflow = single_node_workflow()
        overflow_count = 5
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
            persistent_logger = PersistentRunLogger(output)
            persistent_logger.start(
                RunContext(
                    workflow_topology=topology_from_workflow(workflow),
                    run_id=output.run_id,
                    refresh_per_second=0,
                )
            )
            persistent_logger.record_event(
                make_execution_event(
                    event_type="invocation_finished",
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                    node_id="node.a",
                    provider="alpha",
                    role="executor",
                    task_id="alpha_executor_0",
                    attempt_count=1,
                    cli_captured=True,
                    output_extraction_status="success",
                    provider_usage_status="full",
                    provider_tokens={
                        "input": 100,
                        "cached_input": None,
                        "cache_write": None,
                        "output": 20,
                        "reasoning": None,
                        "total": None,
                    },
                    visible_estimate_tokens=50,
                    visible_estimate_method="char-count-lower-bound",
                    visible_estimate_is_lower_bound=True,
                    configured_cost_usd=0.0004,
                    invocation_cost_confidence="full",
                )
            )
            for index in range(MAX_RETAINED_SUMMARY_EVENTS + overflow_count):
                persistent_logger.record_event(
                    make_execution_event(
                        event_type="runtime_log",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        level="warning",
                        message=f"summary warning {index}",
                        operation="summary_retention_test",
                    )
                )

            persistent_logger.stop(RunResult(status="succeeded"))

            self.assertEqual(
                persistent_logger.dropped_event_count,
                overflow_count + 1,
            )
            summary = persistent_logger.last_summary
            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertIsNotNone(summary.spend)
            assert summary.spend is not None
            self.assertEqual(summary.spend.terminal_invocations, 1)
            self.assertEqual(summary.spend.provider_usage_full_invocations, 1)
            self.assertEqual(summary.spend.configured_cost_usd, 0.0004)
            self.assertEqual(len(summary.provider_rollups), 1)
            self.assertEqual(summary.provider_rollups[0].provider, "alpha")

    def test_invocation_usage_details_are_bounded_without_losing_rollups(
        self,
    ) -> None:
        workflow = single_node_workflow()
        overflow_count = 7
        invocation_count = MAX_RETAINED_INVOCATION_USAGE_DETAILS + overflow_count
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
            persistent_logger = PersistentRunLogger(output)
            persistent_logger.start(
                RunContext(
                    workflow_topology=topology_from_workflow(workflow),
                    run_id=output.run_id,
                    refresh_per_second=0,
                )
            )
            for index in range(invocation_count):
                persistent_logger.record_event(
                    make_execution_event(
                        event_type="invocation_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                        provider="alpha",
                        role="executor",
                        task_id=f"alpha_task_{index:04d}",
                        attempt_count=1,
                        cli_captured=True,
                        output_extraction_status="success",
                        provider_usage_status="full",
                        provider_tokens={
                            "input": 10,
                            "cached_input": None,
                            "cache_write": None,
                            "output": 2,
                            "reasoning": None,
                            "total": None,
                        },
                        visible_estimate_tokens=12,
                        visible_estimate_method="char-count-lower-bound",
                        visible_estimate_is_lower_bound=True,
                        configured_cost_usd=0.0001,
                        invocation_cost_confidence="full",
                    )
                )

            persistent_logger.stop(RunResult(status="succeeded"))

            summary = persistent_logger.last_summary
            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(
                len(summary.invocation_usages),
                MAX_RETAINED_INVOCATION_USAGE_DETAILS,
            )
            self.assertEqual(summary.omitted_invocation_usage_count, overflow_count)
            self.assertIsNotNone(summary.spend)
            assert summary.spend is not None
            self.assertEqual(summary.spend.terminal_invocations, invocation_count)
            self.assertEqual(summary.spend.total_attempts, invocation_count)
            self.assertEqual(
                summary.spend.provider_usage_full_invocations, invocation_count
            )
            self.assertEqual(len(summary.provider_rollups), 1)
            self.assertEqual(
                summary.provider_rollups[0].terminal_invocations,
                invocation_count,
            )
            self.assertEqual(
                summary.invocation_usages[0].task_id,
                f"alpha_task_{overflow_count:04d}",
            )

            summary_text = output.get_orchestrator_summary_path().read_text(
                encoding="utf-8"
            )
            self.assertIn(f"- Terminal invocations: {invocation_count}", summary_text)
            self.assertIn(
                "Invocation detail: retained latest "
                f"{MAX_RETAINED_INVOCATION_USAGE_DETAILS} invocation(s); "
                f"{overflow_count} earlier invocation detail(s)",
                summary_text,
            )
            self.assertNotIn("alpha_task_0000", summary_text)
            self.assertIn(f"alpha_task_{invocation_count - 1:04d}", summary_text)
            event_log_lines = (
                output.get_orchestrator_event_log_path()
                .read_text(encoding="utf-8")
                .splitlines()
            )
            self.assertEqual(len(event_log_lines), invocation_count)

            terminal_summary = render_run_summary_terminal(summary)
            self.assertIn(
                "Invocation detail: retained latest "
                f"{MAX_RETAINED_INVOCATION_USAGE_DETAILS} invocation(s); "
                f"{overflow_count} earlier omitted",
                terminal_summary,
            )

    def test_persistent_run_logger_is_one_shot(self) -> None:
        workflow = single_node_workflow()
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
            persistent_logger = PersistentRunLogger(output)
            context = RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                refresh_per_second=0,
            )

            persistent_logger.start(context)
            persistent_logger.stop(RunResult(status="succeeded"))

            with self.assertRaisesRegex(RuntimeError, "cannot be restarted"):
                persistent_logger.start(context)

    def test_persistent_run_logger_allows_post_stop_failure_summary_event(self) -> None:
        workflow = single_node_workflow()
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = OutputManager(workflow.name, base_dir=Path(tmp_dir))
            persistent_logger = PersistentRunLogger(output)
            persistent_logger.start(
                RunContext(
                    workflow_topology=topology_from_workflow(workflow),
                    run_id=output.run_id,
                    refresh_per_second=0,
                )
            )
            persistent_logger.stop(RunResult(status="failed"))

            persistent_logger.record_event(
                make_execution_event(
                    event_type="runtime_log",
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                    level="warning",
                    message="ignored after stop",
                    operation="runtime_warning",
                )
            )
            persistent_logger.record_failure_summary_event(
                workflow_name=workflow.name,
                run_id=output.run_id,
                message="failure after stop",
            )
            summary = persistent_logger.refresh_summary(RunResult(status="failed"))

            self.assertIsNotNone(summary)
            assert summary is not None
            issue_messages = [issue.message for issue in summary.issues]
            self.assertIn("[error] failure after stop", issue_messages)
            self.assertNotIn("ignored after stop", issue_messages)

    def test_persistent_run_summary_labels_audit_round_invocations(self) -> None:
        workflow = WorkflowPlan(
            name="audit.summary",
            nodes=[
                WorkflowNode(
                    id="review.iterate",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="Review")],
                    providers=[
                        ProviderSpec(provider="codex", role="executor"),
                        ProviderSpec(provider="claude", role="reviewer"),
                    ],
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output = OutputManager(
                workflow.name,
                base_dir=tmp_path,
                log_cli_output=True,
            )
            stage_dir = output.create_stage_dir("review.iterate")
            persistent_logger = PersistentRunLogger(output)

            with ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id=output.run_id,
                observers=[persistent_logger],
                refresh_per_second=0,
            ) as hub:
                hub.emit(
                    make_execution_event(
                        event_type="workflow_started",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="node_started",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="review.iterate",
                    )
                )
                for audit_round_num in (1, 2):
                    output_file = (
                        stage_dir
                        / f"review-audit-round-{audit_round_num}"
                        / "claude_reviewer_0_round1.md"
                    )
                    log_file = output.get_log_file(
                        stage_name="review.iterate",
                        provider="claude",
                        task_id="claude_reviewer_0",
                        audit_round_num=audit_round_num,
                        round_num=1,
                    )
                    hub.emit(
                        make_execution_event(
                            event_type="invocation_started",
                            workflow_name=workflow.name,
                            run_id=output.run_id,
                            node_id="review.iterate",
                            provider="claude",
                            role="reviewer",
                            model="claude-model",
                            task_id="claude_reviewer_0",
                            audit_round_num=audit_round_num,
                            round_num=1,
                            output_file=str(output_file),
                            log_file=str(log_file),
                        )
                    )
                    hub.emit(
                        make_execution_event(
                            event_type="invocation_finished",
                            workflow_name=workflow.name,
                            run_id=output.run_id,
                            node_id="review.iterate",
                            provider="claude",
                            role="reviewer",
                            model="claude-model",
                            task_id="claude_reviewer_0",
                            audit_round_num=audit_round_num,
                            round_num=1,
                            duration_ms=100,
                            attempt_count=1,
                            cli_captured=True,
                            output_extraction_status="success",
                            provider_usage_status="none",
                            provider_tokens={
                                "input": None,
                                "cached_input": None,
                                "cache_write": None,
                                "output": None,
                                "reasoning": None,
                                "total": None,
                            },
                            visible_estimate_tokens=5,
                            visible_estimate_method="char-count-lower-bound",
                            visible_estimate_is_lower_bound=True,
                            invocation_cost_confidence="none",
                        )
                    )
                hub.emit(
                    make_execution_event(
                        event_type="node_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="review.iterate",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="runtime_log",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="review.iterate",
                        audit_round_num=2,
                        round_num=1,
                        level="warning",
                        message="review round warning",
                        operation="review_stall_detection",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="workflow_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                    )
                )

            summary_text = output.get_orchestrator_summary_path().read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "`review.iterate` / `claude_reviewer_0` / `audit1/round1`",
                summary_text,
            )
            self.assertIn(
                "`review.iterate` / `claude_reviewer_0` / `audit2/round1`",
                summary_text,
            )
            self.assertIn("round: audit2/round1", summary_text)
            self.assertEqual(summary_text.count("`claude_reviewer_0`"), 4)
