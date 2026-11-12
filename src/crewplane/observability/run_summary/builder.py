from __future__ import annotations

from math import isfinite

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.architecture.ports import RunSummaryArtifactReaderPort
from crewplane.core.value_checks import is_nonnegative_int
from crewplane.observability.events import ExecutionEvent
from crewplane.observability.types import DashboardSnapshot, RunResult

from .accumulator import RunSummaryAccumulator
from .issues import issue_summaries
from .models import (
    ArtifactReferenceSummary,
    InvocationTimingSummary,
    IssueSummary,
    NodeCounts,
    NodeOutcomeSummary,
    ProviderTokenAggregates,
    RunSummary,
    RunSummaryFacts,
)
from .spend import provider_token_aggregates as build_provider_token_aggregates
from .workspace import build_workspace_run_summary


def build_run_summary(
    artifact_store: RunSummaryArtifactReaderPort,
    snapshot: DashboardSnapshot | None,
    events: list[ExecutionEvent],
    result: RunResult,
    fallback_workflow_name: str,
    fallback_run_id: str,
    dropped_event_count: int = 0,
    summary_facts: RunSummaryFacts | None = None,
    token_aggregates: ProviderTokenAggregates | None = None,
    node_requests: tuple[NodeArtifactRequest, ...] = (),
) -> RunSummary:
    """Build a durable run summary from artifacts, events, and the latest snapshot."""

    workflow_name = (
        snapshot.state.workflow_name if snapshot is not None else fallback_workflow_name
    )
    run_id = snapshot.state.run_id if snapshot is not None else fallback_run_id
    facts = summary_facts or run_summary_facts_from_events(events)
    exact_token_aggregates = token_aggregates or build_provider_token_aggregates(events)
    invocation_usages = facts.invocation_usages
    return RunSummary(
        workflow_name=workflow_name,
        run_id=run_id,
        workflow_status=workflow_status(snapshot, result),
        review_consensus_unresolved=facts.review_consensus_unresolved,
        started_at=facts.started_at,
        completed_at=facts.completed_at,
        elapsed_seconds=elapsed_seconds(snapshot),
        invocation_count=(
            sum(len(node.invocations) for node in snapshot.state.nodes.values())
            if snapshot is not None
            else None
        ),
        invocation_timings=invocation_timing_summaries(snapshot),
        node_counts=node_counts(snapshot),
        spend=facts.spend,
        provider_rollups=facts.provider_rollups,
        invocation_usages=invocation_usages,
        omitted_invocation_usage_count=facts.omitted_invocation_usage_count,
        node_outcomes=node_outcome_summaries(artifact_store, snapshot, facts),
        workspace=build_workspace_run_summary(
            artifact_store,
            facts.workspace_invocations,
            node_requests,
        ),
        issues=summary_issues(
            artifact_store=artifact_store,
            events=events,
            dropped_event_count=dropped_event_count,
        ),
        artifact_references=artifact_reference_summaries(snapshot),
        event_log_path=artifact_store.get_run_event_log_path(),
        summary_path=artifact_store.get_run_summary_path(),
        provider_token_aggregates=exact_token_aggregates,
    )


def run_summary_facts_from_events(events: list[ExecutionEvent]) -> RunSummaryFacts:
    accumulator = RunSummaryAccumulator()
    for event in events:
        accumulator.record(event)
    return accumulator.snapshot()


def summary_issues(
    artifact_store: RunSummaryArtifactReaderPort,
    events: list[ExecutionEvent],
    dropped_event_count: int,
) -> tuple[IssueSummary, ...]:
    issues = issue_summaries(events)
    if dropped_event_count == 0:
        return issues
    return (
        *issues,
        IssueSummary(
            level="warning",
            timestamp_utc="n/a",
            message=(
                "[warning] Summary detail retained the latest "
                f"{len(events)} event(s); {dropped_event_count} earlier event(s) "
                "were omitted from in-memory summary detail. Full events remain in "
                f"{artifact_store.get_run_event_log_path()}."
            ),
        ),
    )


def workflow_status(snapshot: DashboardSnapshot | None, result: RunResult) -> str:
    if result.status != "succeeded":
        return result.status
    if snapshot is not None:
        status = snapshot.state.workflow_status
        if status in {"pending", "running"}:
            return result.status
        return status
    return result.status


def recorded_duration(
    started_at: float | None, finished_at: float | None
) -> float | None:
    if (
        started_at is None
        or finished_at is None
        or not isfinite(started_at)
        or not isfinite(finished_at)
        or finished_at < started_at
    ):
        return None
    return finished_at - started_at


def elapsed_seconds(snapshot: DashboardSnapshot | None) -> float | None:
    if snapshot is None:
        return None
    return recorded_duration(
        snapshot.state.workflow_started_at, snapshot.state.workflow_finished_at
    )


def node_counts(snapshot: DashboardSnapshot | None) -> NodeCounts | None:
    if snapshot is None:
        return None
    state = snapshot.state
    return NodeCounts(
        pending=state.pending_nodes,
        running=state.running_nodes,
        succeeded=state.succeeded_nodes,
        blocked=state.blocked_nodes,
        failed=state.failed_nodes,
        cancelled=sum(node.status == "cancelled" for node in state.nodes.values()),
    )


def node_outcome_summaries(
    artifact_store: RunSummaryArtifactReaderPort,
    snapshot: DashboardSnapshot | None,
    facts: RunSummaryFacts,
) -> tuple[NodeOutcomeSummary, ...]:
    if snapshot is None:
        return ()
    ordered_nodes = sorted(
        snapshot.state.nodes.values(),
        key=lambda node: snapshot.state.node_order[node.node_id],
    )
    summaries: list[NodeOutcomeSummary] = []
    for node in ordered_nodes:
        request = artifact_store.get_node_artifact_request(node.node_id)
        result_path = (
            artifact_store.get_node_output_path(request)
            if request is not None
            else None
        )
        summaries.append(
            NodeOutcomeSummary(
                node_id=node.node_id,
                status=node.status,
                duration_seconds=(
                    None
                    if node.node_id in facts.reused_node_ids
                    else recorded_duration(node.started_at, node.finished_at)
                ),
                resume_kind=(
                    "reused"
                    if node.node_id in facts.reused_node_ids
                    else "checkpoint"
                    if node.node_id in facts.checkpoint_resumed_node_ids
                    else None
                ),
                result_path=(
                    result_path
                    if result_path is not None and result_path.exists()
                    else None
                ),
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


def invocation_timing_summaries(
    snapshot: DashboardSnapshot | None,
) -> tuple[InvocationTimingSummary, ...]:
    if snapshot is None:
        return ()
    return tuple(
        InvocationTimingSummary(
            node_id=node.node_id,
            task_id=invocation.task_id,
            provider=invocation.provider,
            model=invocation.model,
            audit_round_num=invocation.audit_round_num,
            round_num=invocation.round_num,
            status=invocation.status,
            duration_ms=(
                invocation.duration_ms
                if is_nonnegative_int(invocation.duration_ms)
                else None
            ),
        )
        for node in sorted(
            snapshot.state.nodes.values(),
            key=lambda item: snapshot.state.node_order[item.node_id],
        )
        for invocation in node.invocations.values()
    )
