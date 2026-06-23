from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.observability.events import ExecutionEvent
from crewplane.observability.events.payloads import WorkspaceEventPayload

from . import workspace_readers as _workspace_readers
from .models import (
    WorkspaceInvocationSourceSummary,
    WorkspaceInvocationSummary,
    WorkspaceRunSummary,
)
from .workspace_values import artifact_path_label

workspace_descriptor = _workspace_readers.workspace_descriptor
workspace_plan_summary = _workspace_readers.workspace_plan_summary
workspace_state_summaries = _workspace_readers.workspace_state_summaries
workspace_state_summary = _workspace_readers.workspace_state_summary


def workspace_invocation_summary_from_event(
    event: ExecutionEvent,
) -> WorkspaceInvocationSummary | None:
    if event.event_type != "workspace_context_recorded":
        return None
    if not isinstance(event.payload, WorkspaceEventPayload):
        return None
    context = event.context
    payload = event.payload
    if payload.workspace_kind is None and payload.worktree_contract_mode is None:
        return None
    return WorkspaceInvocationSummary(
        node_id=context.node_id,
        task_id=context.task_id,
        audit_round_num=context.audit_round_num,
        round_num=context.round_num,
        workspace_kind=payload.workspace_kind,
        logical_worktree_name=payload.workspace_logical_worktree_name,
        status=payload.status,
        state_path=payload.workspace_state_path,
        writable=payload.workspace_writable,
        lineage_producer=payload.workspace_lineage_producer,
        child_environment_required=payload.workspace_child_environment_required,
        child_environment_applied=payload.workspace_child_environment_applied,
        source=WorkspaceInvocationSourceSummary(
            kind=payload.workspace_source_kind,
            node_id=payload.workspace_source_node_id,
            commit=payload.workspace_source_commit,
            tree=payload.workspace_source_tree,
            worktree_contract_mode=payload.worktree_contract_mode,
            worktree_contract_schema_version=payload.worktree_contract_schema_version,
        ),
        materialization=payload.workspace_materialization,
    )


def build_workspace_run_summary(
    artifact_store: ArtifactStorePort,
    event_invocations: tuple[WorkspaceInvocationSummary, ...],
) -> WorkspaceRunSummary | None:
    plan = workspace_plan_summary(artifact_store.stages_dir)
    states = workspace_state_summaries(artifact_store.stages_dir)
    invocations = merge_workspace_invocations(
        artifact_store.stages_dir,
        event_invocations,
        states,
    )
    if plan is None and not invocations:
        return None
    return WorkspaceRunSummary(plan=plan, invocations=invocations)


def merge_workspace_invocations(
    stages_dir: Path,
    event_invocations: tuple[WorkspaceInvocationSummary, ...],
    state_invocations: tuple[WorkspaceInvocationSummary, ...],
) -> tuple[WorkspaceInvocationSummary, ...]:
    merged: dict[tuple[object, ...], WorkspaceInvocationSummary] = {}
    for invocation in event_invocations:
        key = workspace_invocation_key(invocation)
        merged[key] = replace(
            invocation,
            state_path=artifact_path_label(stages_dir, invocation.state_path),
        )
    for invocation in state_invocations:
        key = workspace_invocation_key(invocation)
        existing = merged.get(key)
        merged[key] = merge_workspace_invocation(existing, invocation)
    return with_checkpoint_counts(tuple(merged.values()))


def merge_workspace_invocation(
    existing: WorkspaceInvocationSummary | None,
    state: WorkspaceInvocationSummary,
) -> WorkspaceInvocationSummary:
    if existing is None:
        return state
    return replace(
        state,
        status=merged_workspace_status(existing.status, state.status),
        state_path=_existing_value_or_state(existing.state_path, state.state_path),
        child_environment_required=_state_value_or_existing(
            state.child_environment_required,
            existing.child_environment_required,
        ),
        child_environment_applied=_state_value_or_existing(
            state.child_environment_applied,
            existing.child_environment_applied,
        ),
        writable=_state_value_or_existing(state.writable, existing.writable),
        lineage_producer=_state_value_or_existing(
            state.lineage_producer,
            existing.lineage_producer,
        ),
    )


def _existing_value_or_state[T](
    existing_value: T | None,
    state_value: T | None,
) -> T | None:
    return existing_value if existing_value is not None else state_value


def _state_value_or_existing[T](
    state_value: T | None,
    existing_value: T | None,
) -> T | None:
    return state_value if state_value is not None else existing_value


def merged_workspace_status(
    existing_status: str | None,
    state_status: str | None,
) -> str | None:
    if existing_status == "running" and is_terminal_workspace_status(state_status):
        return state_status
    if existing_status is not None:
        return existing_status
    return state_status


def is_terminal_workspace_status(status: str | None) -> bool:
    return status in {"succeeded", "failed", "cancelled", "timed_out"}


def with_checkpoint_counts(
    invocations: tuple[WorkspaceInvocationSummary, ...],
) -> tuple[WorkspaceInvocationSummary, ...]:
    counts: dict[str, int] = {}
    for invocation in invocations:
        if not is_worktree_checkpoint(invocation):
            continue
        name = invocation.logical_worktree_name
        if name is not None:
            counts[name] = counts.get(name, 0) + 1
    return tuple(
        replace(
            invocation,
            checkpoint_count=counts.get(invocation.logical_worktree_name),
        )
        if invocation.logical_worktree_name is not None
        else invocation
        for invocation in invocations
    )


def is_worktree_checkpoint(invocation: WorkspaceInvocationSummary) -> bool:
    return (
        invocation.workspace_kind == "worktree"
        and invocation.lineage_producer is True
        and invocation.result_commit is not None
    )


def workspace_invocation_key(
    summary: WorkspaceInvocationSummary,
) -> tuple[object, ...]:
    return (
        summary.node_id,
        summary.task_id,
        summary.audit_round_num,
        summary.round_num,
    )
