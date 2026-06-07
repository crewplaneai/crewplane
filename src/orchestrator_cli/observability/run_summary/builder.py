from __future__ import annotations

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.observability.events import ExecutionEvent
from orchestrator_cli.observability.timing import format_elapsed_seconds
from orchestrator_cli.observability.types import DashboardSnapshot, RunResult

from .formatting import duration_label
from .issues import issue_summaries
from .models import (
    ArtifactReferenceSummary,
    NodeCounts,
    NodeOutcomeSummary,
    RunSummary,
)
from .spend import (
    invocation_usage_summaries,
    provider_usage_rollups,
    spend_totals,
)


def build_run_summary(
    artifact_store: ArtifactStorePort,
    snapshot: DashboardSnapshot | None,
    events: list[ExecutionEvent],
    result: RunResult,
    fallback_workflow_name: str,
    fallback_run_id: str,
) -> RunSummary:
    workflow_name = (
        snapshot.state.workflow_name if snapshot is not None else fallback_workflow_name
    )
    run_id = snapshot.state.run_id if snapshot is not None else fallback_run_id
    invocation_usages = invocation_usage_summaries(events)
    return RunSummary(
        workflow_name=workflow_name,
        run_id=run_id,
        workflow_status=workflow_status(snapshot, result),
        review_consensus_unresolved=review_consensus_unresolved(events),
        started_at=event_time(events, "workflow_started"),
        completed_at=event_time(events, "workflow_finished", "workflow_failed"),
        elapsed_label=elapsed_label(snapshot),
        node_counts=node_counts(snapshot),
        spend=spend_totals(invocation_usages),
        provider_rollups=provider_usage_rollups(invocation_usages),
        invocation_usages=invocation_usages,
        node_outcomes=node_outcome_summaries(artifact_store, snapshot),
        issues=issue_summaries(events),
        artifact_references=artifact_reference_summaries(snapshot),
        event_log_path=artifact_store.get_orchestrator_event_log_path(),
        summary_path=artifact_store.get_orchestrator_summary_path(),
    )


def workflow_status(snapshot: DashboardSnapshot | None, result: RunResult) -> str:
    if snapshot is not None:
        status = snapshot.state.workflow_status
        if status in {"pending", "running"}:
            return "failed" if result.failed else "succeeded"
        return status
    return "failed" if result.failed else "succeeded"


def review_consensus_unresolved(events: list[ExecutionEvent]) -> bool:
    return any(
        event.event_type == "runtime_log"
        and event.operation == "review_loop_consensus_exhausted"
        and event.attributes is not None
        and event.attributes.get("continued") is True
        for event in events
    )


def elapsed_label(snapshot: DashboardSnapshot | None) -> str | None:
    if snapshot is None:
        return None
    return format_elapsed_seconds(snapshot.state.elapsed_seconds)


def node_counts(snapshot: DashboardSnapshot | None) -> NodeCounts:
    if snapshot is None:
        return NodeCounts(pending=0, running=0, succeeded=0, blocked=0, failed=0)
    state = snapshot.state
    return NodeCounts(
        pending=state.pending_nodes,
        running=state.running_nodes,
        succeeded=state.succeeded_nodes,
        blocked=state.blocked_nodes,
        failed=state.failed_nodes,
    )


def event_time(events: list[ExecutionEvent], *event_types: str) -> str:
    for event in reversed(events):
        if event.event_type in event_types:
            return event.timestamp_utc
    return "n/a"


def node_outcome_summaries(
    artifact_store: ArtifactStorePort,
    snapshot: DashboardSnapshot | None,
) -> tuple[NodeOutcomeSummary, ...]:
    if snapshot is None:
        return ()
    ordered_nodes = sorted(
        snapshot.state.nodes.values(),
        key=lambda node: snapshot.state.node_order[node.node_id],
    )
    summaries: list[NodeOutcomeSummary] = []
    for node in ordered_nodes:
        result_path = artifact_store.get_stage_output_path(node.node_id)
        summaries.append(
            NodeOutcomeSummary(
                node_id=node.node_id,
                status=node.status,
                duration_label=duration_label(
                    node.started_at,
                    node.finished_at,
                    snapshot.now,
                ),
                result_path=result_path if result_path.exists() else None,
            )
        )
    return tuple(summaries)


def artifact_reference_summaries(
    snapshot: DashboardSnapshot | None,
) -> tuple[ArtifactReferenceSummary, ...]:
    if snapshot is None:
        return ()
    ordered_nodes = sorted(
        snapshot.state.nodes.values(),
        key=lambda node: snapshot.state.node_order[node.node_id],
    )
    references: list[ArtifactReferenceSummary] = []
    for node in ordered_nodes:
        for invocation in node.invocations.values():
            references.append(
                ArtifactReferenceSummary(
                    node_id=node.node_id,
                    task_id=invocation.task_id,
                    audit_round_num=invocation.audit_round_num,
                    round_num=invocation.round_num,
                    output_file=invocation.output_file,
                    log_file=invocation.log_file,
                )
            )
    return tuple(references)
