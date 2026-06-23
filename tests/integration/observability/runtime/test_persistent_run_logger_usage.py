import tempfile
import unittest
from pathlib import Path

from crewplane.artifacts import OutputManager
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability import PersistentRunLogger
from crewplane.observability.persistent import render_run_summary_terminal
from crewplane.observability.runtime import ObservabilityHub
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)
from tests.integration.observability.runtime.observability_runtime_helpers import (
    single_node_workflow,
    two_node_workflow,
)


class PersistentRunLoggerUsageTests(unittest.TestCase):
    def test_persistent_run_logger_records_failed_invocation_usage(self) -> None:
        workflow = single_node_workflow()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output = OutputManager(
                workflow.name, base_dir=tmp_path, log_cli_output=True
            )
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
                        node_id="node.a",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="invocation_failed",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                        provider="alpha",
                        role=ProviderRole.EXECUTOR,
                        model="alpha-model",
                        task_id="alpha_executor_0",
                        round_num=1,
                        duration_ms=250,
                        error="boom",
                        attempt_count=1,
                        cli_captured=False,
                        output_extraction_status="missing",
                        provider_usage_status="none",
                        provider_tokens={
                            "input": None,
                            "cached_input": None,
                            "cache_write": None,
                            "output": None,
                            "reasoning": None,
                            "total": None,
                        },
                        visible_estimate_tokens=6,
                        visible_estimate_method="char-count-lower-bound",
                        visible_estimate_is_lower_bound=True,
                        configured_cost_usd=0.000007,
                        invocation_cost_confidence="partial",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="node_failed",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                        error="boom",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="workflow_failed",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        error="boom",
                    )
                )

            summary_text = output.get_run_summary_path().read_text(encoding="utf-8")
            self.assertIn("`node.a` / `alpha_executor_0`", summary_text)
            self.assertIn("output extraction: missing", summary_text)
            self.assertIn("configured cost: $0.000007", summary_text)
            self.assertIn("`node.a`: failed", summary_text)
            self.assertIn("result: not produced", summary_text)
            self.assertNotIn(str(output.get_stage_output_path("node.a")), summary_text)
            last_summary = persistent_logger.last_summary
            self.assertIsNotNone(last_summary)
            assert last_summary is not None
            terminal_summary = render_run_summary_terminal(last_summary)
            self.assertIn("Status: failed", terminal_summary)
            self.assertIn(
                "Provider token reports: 0/1 full, 0/1 partial, 0/1 malformed",
                terminal_summary,
            )
            self.assertIn(
                "Configured cost estimate: $0.000007 (partial)", terminal_summary
            )

    def test_persistent_run_summary_does_not_link_missing_blocked_result(
        self,
    ) -> None:
        workflow = two_node_workflow()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output = OutputManager(
                workflow.name, base_dir=tmp_path, log_cli_output=True
            )
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
                        event_type="node_blocked",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.b",
                        error="unsatisfied dependencies: node.a",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="workflow_failed",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        error="failed",
                    )
                )

            summary_text = output.get_run_summary_path().read_text(encoding="utf-8")
            self.assertIn("`node.b`: blocked", summary_text)
            self.assertIn("result: not produced", summary_text)
            self.assertIn("[error] Workflow failed: failed", summary_text)
            self.assertNotIn("Workflow failed: unspecified error", summary_text)
            self.assertNotIn(str(output.get_stage_output_path("node.b")), summary_text)

    def test_persistent_run_logger_counts_partial_provider_token_reports(
        self,
    ) -> None:
        workflow = single_node_workflow()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output = OutputManager(
                workflow.name, base_dir=tmp_path, log_cli_output=True
            )
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
                        node_id="node.a",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="invocation_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                        provider="alpha",
                        role=ProviderRole.EXECUTOR,
                        model="alpha-model",
                        task_id="alpha_executor_0",
                        round_num=1,
                        duration_ms=250,
                        attempt_count=1,
                        cli_captured=True,
                        output_extraction_status="success",
                        provider_usage_status="partial",
                        provider_tokens={
                            "input": 120,
                            "cached_input": None,
                            "cache_write": None,
                            "output": None,
                            "reasoning": None,
                            "total": None,
                        },
                        visible_estimate_tokens=8,
                        visible_estimate_method="char-count-lower-bound",
                        visible_estimate_is_lower_bound=True,
                        configured_cost_usd=0.000025,
                        invocation_cost_confidence="partial",
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

            summary_text = output.get_run_summary_path().read_text(encoding="utf-8")
            self.assertIn(
                "Provider token reports: 0/1 full, 1/1 partial, 0/1 malformed",
                summary_text,
            )
            self.assertIn(
                "provider tokens: input=120, cached_input=n/a, cache_write=n/a, output=n/a, reasoning=n/a, total=n/a",
                summary_text,
            )
            last_summary = persistent_logger.last_summary
            self.assertIsNotNone(last_summary)
            assert last_summary is not None
            terminal_summary = render_run_summary_terminal(last_summary)
            self.assertIn(
                "Provider token reports: 0/1 full, 1/1 partial, 0/1 malformed",
                terminal_summary,
            )
            self.assertIn("cost $0.000025 (partial)", terminal_summary)

    def test_persistent_run_logger_renders_output_only_provider_totals(self) -> None:
        workflow = single_node_workflow()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output = OutputManager(
                workflow.name, base_dir=tmp_path, log_cli_output=True
            )
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
                        node_id="node.a",
                    )
                )
                hub.emit(
                    make_execution_event(
                        event_type="invocation_finished",
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="node.a",
                        provider="alpha",
                        role=ProviderRole.EXECUTOR,
                        model="alpha-model",
                        task_id="alpha_executor_0",
                        round_num=1,
                        duration_ms=250,
                        attempt_count=1,
                        cli_captured=True,
                        output_extraction_status="success",
                        provider_usage_status="partial",
                        provider_tokens={
                            "input": None,
                            "cached_input": None,
                            "cache_write": None,
                            "output": 45,
                            "reasoning": None,
                            "total": None,
                        },
                        visible_estimate_tokens=8,
                        visible_estimate_method="char-count-lower-bound",
                        visible_estimate_is_lower_bound=True,
                        configured_cost_usd=0.000025,
                        invocation_cost_confidence="partial",
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

            summary_text = output.get_run_summary_path().read_text(encoding="utf-8")
            self.assertIn(
                "provider tokens: input=n/a, cached_input=n/a, cache_write=n/a, output=45, reasoning=n/a, total=n/a",
                summary_text,
            )
            last_summary = persistent_logger.last_summary
            self.assertIsNotNone(last_summary)
            assert last_summary is not None
            terminal_summary = render_run_summary_terminal(last_summary)
            self.assertIn(
                "Provider token reports: 0/1 full, 1/1 partial, 0/1 malformed",
                terminal_summary,
            )
            self.assertIn("cost $0.000025 (partial)", terminal_summary)
