import tempfile
from pathlib import Path

import pytest

from crewplane.architecture.contracts import EventType
from crewplane.artifacts import OutputManager
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability import PersistentRunLogger
from crewplane.observability.events import (
    apply_event,
    build_initial_state,
)
from crewplane.observability.persistent import (
    render_run_summary_terminal,
)
from crewplane.observability.runtime import ObservabilityHub
from crewplane.observability.types import (
    RunContext,
    RunResult,
)
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)
from tests.integration.observability.runtime.observability_runtime_helpers import (
    RecordingObserver,
    single_node_workflow,
)


def test_runtime_log_warning_updates_recent_events() -> None:
    workflow = single_node_workflow()
    state = build_initial_state(topology_from_workflow(workflow), run_id="run-1")

    apply_event(
        state,
        make_execution_event(
            event_type=EventType.RUNTIME_LOG,
            workflow_name=workflow.name,
            run_id="run-1",
            node_id="node.a",
            level="warning",
            message="used stderr as output",
            operation="stderr_fallback",
        ),
    )

    assert "WARN used stderr as output" in list(state.nodes["node.a"].recent_events)


def test_persistent_run_logger_writes_ndjson_and_summary() -> None:
    workflow = single_node_workflow()
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        output = OutputManager(workflow.name, base_dir=tmp_path, log_cli_output=True)
        stage_dir = output.create_node_dir(node_artifact_request("node.a"))
        output_file = stage_dir / "alpha_executor_0_round1.md"
        log_file = output.get_node_log_file(
            node_artifact_request("node.a"),
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
                    event_type=EventType.WORKFLOW_STARTED,
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                )
            )
            hub.emit(
                make_execution_event(
                    event_type=EventType.NODE_STARTED,
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                    node_id="node.a",
                )
            )
            hub.emit(
                make_execution_event(
                    event_type=EventType.INVOCATION_STARTED,
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                    node_id="node.a",
                    provider="alpha",
                    role=ProviderRole.EXECUTOR,
                    model="alpha-model",
                    task_id="alpha_executor_0",
                    round_num=1,
                    output_file=str(output_file),
                    log_file=str(log_file),
                )
            )
            hub.emit(
                make_execution_event(
                    event_type=EventType.RUNTIME_LOG,
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
                    event_type=EventType.INVOCATION_FINISHED,
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                    node_id="node.a",
                    provider="alpha",
                    role=ProviderRole.EXECUTOR,
                    model="alpha-model",
                    task_id="alpha_executor_0",
                    round_num=1,
                    duration_ms=250,
                    attempt_count=2,
                    cli_captured=True,
                    output_extraction_status="success",
                    provider_usage_status="full",
                    provider_usage_report_count=2,
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
                    event_type=EventType.NODE_FINISHED,
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                    node_id="node.a",
                )
            )
            hub.emit(
                make_execution_event(
                    event_type=EventType.WORKFLOW_FINISHED,
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                )
            )

        event_log = output.get_run_event_log_path()
        summary_log = output.get_run_summary_path()
        assert event_log.exists()
        assert summary_log.exists()

        event_log_text = event_log.read_text(encoding="utf-8")
        summary_text = summary_log.read_text(encoding="utf-8")
        assert '"event_type": "runtime_log"' in event_log_text
        assert '"operation": "stderr_fallback"' in event_log_text
        assert (
            "Invocation succeeded with empty stdout; used stderr as output."
            in summary_text
        )
        assert "Provider log contains the original stderr lines" in summary_text
        assert '"attempt_count": 2' in event_log_text
        assert (
            '"provider_tokens": {"cache_write": null, "cached_input": null, "input": 90'
            in event_log_text
        )
        assert "## Spend Observability" in summary_text
        assert "provider report: full" in summary_text
        assert "Configured cost estimate: $0.000207 (confidence: full)" in summary_text
        assert "## Node Outcomes" in summary_text
        assert "## Artifact References" in summary_text
        last_summary = persistent_logger.last_summary
        assert last_summary is not None
        assert last_summary is not None
        terminal_summary = render_run_summary_terminal(last_summary)
        assert (
            "Provider usage status: 1/1 full, 0/1 partial, 0/1 malformed"
            in terminal_summary
        )
        assert "Provider-reported tokens:" not in terminal_summary
        durable_summary = persistent_logger.refresh_summary(
            RunResult(status="succeeded")
        )
        assert durable_summary is not None
        assert durable_summary is not None
        durable_terminal_summary = render_run_summary_terminal(durable_summary)
        assert (
            "Provider-reported tokens: n/a across 2 reports" in durable_terminal_summary
        )
        assert "usage 1/1 full, 0/1 partial, 0/1 malformed" in terminal_summary
        assert "alpha: 1 invocation(s)" in terminal_summary
        assert "Configured cost estimate: $0.000207 (full)" in terminal_summary


def test_persistent_run_logger_is_one_shot() -> None:
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

        with pytest.raises(RuntimeError, match="cannot be restarted"):
            persistent_logger.start(context)


def test_persistent_run_logger_allows_post_stop_failure_summary_event() -> None:
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
                event_type=EventType.RUNTIME_LOG,
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

        assert summary is not None
        assert summary is not None
        issue_messages = [issue.message for issue in summary.issues]
        assert "[error] failure after stop" in issue_messages
        assert "ignored after stop" not in issue_messages


def test_persistent_run_summary_labels_audit_round_invocations() -> None:
    workflow = WorkflowPlan(
        name="audit.summary",
        nodes=[
            WorkflowNode(
                id="review.iterate",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review")
                ],
                providers=[
                    ProviderSpec(provider="codex", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="claude", role=ProviderRole.REVIEWER),
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
        stage_dir = output.create_node_dir(node_artifact_request("review.iterate"))
        persistent_logger = PersistentRunLogger(output)

        with ObservabilityHub(
            workflow_topology=topology_from_workflow(workflow),
            run_id=output.run_id,
            observers=[persistent_logger],
            refresh_per_second=0,
        ) as hub:
            hub.emit(
                make_execution_event(
                    event_type=EventType.WORKFLOW_STARTED,
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                )
            )
            hub.emit(
                make_execution_event(
                    event_type=EventType.NODE_STARTED,
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
                log_file = output.get_node_log_file(
                    node_artifact_request("review.iterate"),
                    provider="claude",
                    task_id="claude_reviewer_0",
                    audit_round_num=audit_round_num,
                    round_num=1,
                )
                hub.emit(
                    make_execution_event(
                        event_type=EventType.INVOCATION_STARTED,
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="review.iterate",
                        provider="claude",
                        role=ProviderRole.REVIEWER,
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
                        event_type=EventType.INVOCATION_FINISHED,
                        workflow_name=workflow.name,
                        run_id=output.run_id,
                        node_id="review.iterate",
                        provider="claude",
                        role=ProviderRole.REVIEWER,
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
                    event_type=EventType.NODE_FINISHED,
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                    node_id="review.iterate",
                )
            )
            hub.emit(
                make_execution_event(
                    event_type=EventType.RUNTIME_LOG,
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
                    event_type=EventType.WORKFLOW_FINISHED,
                    workflow_name=workflow.name,
                    run_id=output.run_id,
                )
            )

        summary_text = output.get_run_summary_path().read_text(encoding="utf-8")
        assert (
            "`review.iterate` / `claude_reviewer_0` / `audit1/round1`" in summary_text
        )
        assert (
            "`review.iterate` / `claude_reviewer_0` / `audit2/round1`" in summary_text
        )
        assert "round: audit2/round1" in summary_text
        assert summary_text.count("`claude_reviewer_0`") == 4
