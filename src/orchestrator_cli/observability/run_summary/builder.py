from __future__ import annotations

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.observability.events import ExecutionEvent
from orchestrator_cli.observability.timing import format_elapsed_seconds
from orchestrator_cli.observability.types import DashboardSnapshot, RunResult

from .accumulator import RunSummaryAccumulator
from .formatting import duration_label
from .issues import issue_summaries
from .models import (
    ArtifactReferenceSummary,
    IssueSummary,
    NodeCounts,
    NodeOutcomeSummary,
    RunSummary,
    RunSummaryFacts,
)


def build_run_summary(
    artifact_store: ArtifactStorePort,
    snapshot: DashboardSnapshot | None,
    events: list[ExecutionEvent],
    result: RunResult,
    fallback_workflow_name: str,
    fallback_run_id: str,
    dropped_event_count: int = 0,
    summary_facts: RunSummaryFacts | None = None,
) -> RunSummary:
    workflow_name = (
        snapshot.state.workflow_name if snapshot is not None else fallback_workflow_name
    )
    run_id = snapshot.state.run_id if snapshot is not None else fallback_run_id
    facts = summary_facts or run_summary_facts_from_events(events)
    invocation_usages = facts.invocation_usages
    return RunSummary(
        workflow_name=workflow_name,
        run_id=run_id,
        workflow_status=workflow_status(snapshot, result),
        review_consensus_unresolved=facts.review_consensus_unresolved,
        started_at=facts.started_at,
        completed_at=facts.completed_at,
        elapsed_label=elapsed_label(snapshot),
        node_counts=node_counts(snapshot),
        spend=facts.spend,
        provider_rollups=facts.provider_rollups,
        invocation_usages=invocation_usages,
        omitted_invocation_usage_count=facts.omitted_invocation_usage_count,
        node_outcomes=node_outcome_summaries(artifact_store, snapshot),
        issues=summary_issues(
            artifact_store=artifact_store,
            events=events,
            dropped_event_count=dropped_event_count,
        ),
        artifact_references=artifact_reference_summaries(snapshot),
        event_log_path=artifact_store.get_orchestrator_event_log_path(),
        summary_path=artifact_store.get_orchestrator_summary_path(),
    )


def run_summary_facts_from_events(events: list[ExecutionEvent]) -> RunSummaryFacts:
    accumulator = RunSummaryAccumulator()
    for event in events:
        accumulator.record(event)
    return accumulator.snapshot()


def summary_issues(
    artifact_store: ArtifactStorePort,
    events: list[ExecutionEvent],
    dropped_event_count: int,
) -> tuple[IssueSummary, ...]:
    issues = issue_summaries(events)
    if dropped_event_count == 0:
        return issues
    return (
        IssueSummary(
            level="warning",
            timestamp_utc="n/a",
            message=(
                "[warning] Summary detail retained the latest "
                f"{len(events)} event(s); {dropped_event_count} earlier event(s) "
                "were omitted from in-memory summary detail. Full events remain in "
                f"{artifact_store.get_orchestrator_event_log_path()}."
            ),
        ),
        *issues,
    )


def workflow_status(snapshot: DashboardSnapshot | None, result: RunResult) -> str:
    if snapshot is not None:
        status = snapshot.state.workflow_status
        if status in {"pending", "running"}:
            return "failed" if result.failed else "succeeded"
        return status
    return "failed" if result.failed else "succeeded"


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
