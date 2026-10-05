"""Publish phase checkpoints and restore runtime registrations and diagnostics."""

from __future__ import annotations

import asyncio
from pathlib import Path

from crewplane.architecture.contracts import LogLevel
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.naming import review_checkpoint_relative_path
from crewplane.artifacts.results.findings import (
    FindingsSelection,
    extract_findings_content,
)
from crewplane.artifacts.resume import checkpoint_hydration
from crewplane.artifacts.resume.checkpoint_validation import (
    require_checkpoint_dependencies,
    require_checkpoint_project,
)
from crewplane.artifacts.workspace import checkpoint_state
from crewplane.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    ReviewCheckpointResumeSummary,
)
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import PreflightExecutionNode
from crewplane.core.review_checkpoint import (
    CheckpointIdentity,
    ClosedReviewCheckpoint,
    OpenReviewCheckpoint,
    ReviewLoopCheckpoint,
    validate_checkpoint_node,
)
from crewplane.core.review_checkpoint_state import (
    CheckpointPhase,
    CheckpointProjectObservation,
    CheckpointTask,
)

from ..common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    RuntimeEventContext,
    build_stage_task_specs,
    emit_runtime_log,
)
from . import checkpoint_dependencies
from .candidate_identity import project_fingerprint
from .checkpoint_progress import ProgressRestorer, encode_progress
from .state import (
    build_review_loop_status_payload,
    persist_review_inbox,
    persist_review_loop_status,
    render_review_inbox,
)
from .types import (
    ReviewLoopProgress,
    ReviewLoopRunContext,
)


def checkpoint_identity(context: ReviewLoopRunContext) -> CheckpointIdentity:
    """Build this run's checkpoint identity in compiled provider order.

    Use the workflow name when runtime identity is absent. Inputs stay unchanged;
    model validation errors propagate without publishing artifacts.
    """
    plan = context.runtime_context.plan
    return CheckpointIdentity(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        plan_schema_version=plan.plan_schema_version,
        workflow_identity=context.runtime_context.workflow_identity
        or plan.workflow_name,
        workflow_name=plan.workflow_name,
        workflow_signature=plan.workflow_signature,
        run_id=context.output.run_id,
        run_key_name=context.output.run_key_name,
        node_id=context.stage.id,
        tasks=[
            CheckpointTask(task_id=p.task_id, role=p.role)
            for p in context.stage.provider_records
        ],
    )


def boundary_observation(
    context: ReviewLoopRunContext,
) -> CheckpointProjectObservation | None:
    """Observe an unmanaged project without changing files or runtime state.

    Managed workspaces return None. Reliability requires a fingerprint and stable
    tracker exclusivity, or single-node scheduling when untracked. Observation
    errors propagate; no checkpoint is published here.
    """
    policy = context.stage.workspace_policy
    if policy is not None and policy.enabled:
        return None
    fingerprint, exclusive = _observe_project(
        context.runtime_context, context.output, context.stage.id, context.telemetry
    )
    if exclusive is None:
        exclusive = (
            context.runtime_context.max_concurrent_nodes() == 1
            or len(context.runtime_context.plan.execution_order) == 1
        )
    return CheckpointProjectObservation(
        fingerprint=fingerprint, reliable=fingerprint is not None and exclusive
    )


def _observe_project(
    runtime: CompiledRuntimeContext,
    output: ArtifactStorePort,
    node_id: str,
    telemetry: ExecutionTelemetry | None,
) -> tuple[str | None, bool | None]:
    """Return the fingerprint and tracker exclusivity, or None when untracked."""
    tracker = None if telemetry is None else telemetry.activity_tracker
    before = None if tracker is None else tracker.snapshot(node_id)
    fingerprint = project_fingerprint(
        Path(runtime.plan.project_root), (output.stages_dir.parent.parent,)
    )
    after = None if tracker is None else tracker.snapshot(node_id)
    exclusive = None if before is None else before.is_exclusive and before == after
    return fingerprint, exclusive


def _prepare_checkpoint(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
    phase: CheckpointPhase,
    local_round: int,
) -> tuple[OpenReviewCheckpoint, checkpoint_state.PreparedCheckpointWorkspaces]:
    root = context.output.stages_dir
    stored = encode_progress(root, progress)
    files, mappings = checkpoint_dependencies.collect_checkpoint_dependencies(
        context, stored
    )
    workspace = checkpoint_state.prepare_checkpoint_workspaces(
        context.output, context.runtime_context.plan, context.stage, stored
    )
    files.extend(workspace.files)
    previous = context.runtime_context.review_checkpoints.get(context.stage.id)
    checkpoint = OpenReviewCheckpoint(
        **checkpoint_identity(context).model_dump(),
        audit=progress.cursor_audit,
        local_round=local_round,
        next_phase=phase,
        progress=stored,
        files=list({item.relative_path: item for item in files}.values()),
        generated_mappings=mappings,
        workspaces=workspace.workspaces,
        project_observation=boundary_observation(context),
        resume_origin=None if previous is None else previous.resume_origin,
    )
    validate_checkpoint_node(checkpoint, context.stage)
    return checkpoint, workspace


async def commit_checkpoint(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
    phase: CheckpointPhase,
    local_round: int,
) -> None:
    """Prepare off-thread, then synchronously publish a completed phase boundary.

    Cancellation at the preparation await prevents publication; an already running
    worker may finish. Publication deliberately has no awaits: the registry lock
    spans workspace snapshots, dependency recovery, marker write, and cursor
    updates. It serializes observers without rolling back on failure.

    Errors propagate with earlier files and registrations retained. A marker
    surviving a failed write is registered, but the in-memory checkpoint and
    cursor advance only after marker publication returns successfully. The caller owns the
    store and registry, including closing its recovery spool.
    """
    checkpoint, workspace = await asyncio.to_thread(
        _prepare_checkpoint, context, progress, phase, local_round
    )
    root = context.output.stages_dir
    publications = context.runtime_context.runtime_publications
    with publications.transaction():
        checkpoint_state.publish_checkpoint_workspaces(root, workspace)
        published, _ = publications.snapshot()
        for descriptor in checkpoint.files:
            dependency = root / descriptor.relative_path
            if published.get(dependency) == descriptor.signature:
                publications.capture_recovery_snapshot(dependency, descriptor.signature)
            else:
                publications.publish(
                    dependency, descriptor.signature, recovery_source=dependency
                )
        _publish_marker(context, checkpoint)
        context.runtime_context.review_checkpoints[context.stage.id] = checkpoint
        progress.next_phase, progress.cursor_round = phase, local_round


def close_checkpoint(context: ReviewLoopRunContext, reason: str) -> None:
    """Publish a terminal marker under the caller-owned registry lock.

    Leave the in-memory open checkpoint untouched. Write errors propagate;
    a marker surviving replacement is still registered for recovery.
    """
    publications = context.runtime_context.runtime_publications
    with publications.transaction():
        checkpoint = ClosedReviewCheckpoint(
            **checkpoint_identity(context).model_dump(), terminal_reason=reason
        )
        _publish_marker(context, checkpoint)


def _publish_marker(
    context: ReviewLoopRunContext, checkpoint: ReviewLoopCheckpoint
) -> None:
    try:
        context.output.write_review_checkpoint(checkpoint)
    finally:
        # Directory sync can fail after replacement; register the marker that survived.
        if context.output.read_review_checkpoint(context.stage.id) == checkpoint:
            path = context.output.stages_dir / review_checkpoint_relative_path(
                context.stage.id
            )
            context.runtime_context.runtime_publications.publish(
                path, file_size_and_sha256(path), recovery_source=path
            )


def validate_final_findings(
    context: ReviewLoopRunContext, progress: ReviewLoopProgress
) -> None:
    """Validate selected latest executor findings in output order without writes.

    Extraction errors propagate at the first invalid output; the caller decides
    whether to close the checkpoint. Inputs and resource ownership stay unchanged.
    """
    selection = FindingsSelection.from_stage(
        build_stage_task_specs(context.stage), context.stage.findings
    )
    for artifact in progress.latest_executor_outputs or []:
        if selection.should_extract(artifact.task_id, artifact.content):
            extract_findings_content(artifact.content, artifact.output_file)


def restore_diagnostics(
    node_dir: Path, node_id: str, progress: ReviewLoopProgress
) -> list[Path]:
    """Write status, then an inbox when selected candidates and reviews permit.

    Prefer active-audit evidence when present. Return written paths in that order
    without registering publications or mutating progress. Errors propagate and
    earlier writes remain; atomic writers own temporary-file cleanup.
    """
    paths = [
        persist_review_loop_status(
            node_dir, build_review_loop_status_payload(node_id, node_dir, progress)
        )
    ]
    active = progress.active_audit
    reviews = (
        progress.latest_reviewer_outputs
        if active is None
        else active.latest_reviewer_outputs
    )
    candidates = (
        progress.latest_executor_outputs
        if active is None
        else active.latest_valid_executor_outputs
    )
    if reviews and candidates:
        first = reviews[0]
        inbox = render_review_inbox(
            node_id,
            first.audit_round_num,
            first.round_num,
            candidates,
            None if active is None else active.previous_executor_outputs,
            reviews,
        )
        if inbox is not None:
            paths.append(
                persist_review_inbox(first.output_file.parent, first.round_num, inbox)
            )
    return paths


def require_entry_project(
    runtime: CompiledRuntimeContext,
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    telemetry: ExecutionTelemetry | None = None,
) -> None:
    """Require unchanged, verifiable project contents for an unmanaged checkpoint.

    Missing checkpoints and managed workspaces skip observation. Without a tracker,
    a fingerprint suffices regardless of scheduling limits; otherwise exclusivity
    must stay stable. ValueError reports a mismatch or unreliable observation.
    Observation errors propagate without changing files or registrations.
    """
    checkpoint = runtime.review_checkpoints.get(node.id)
    if checkpoint is None:
        return
    policy = node.workspace_policy
    if policy is not None and policy.enabled:
        return
    fingerprint, exclusive = _observe_project(runtime, output, node.id, telemetry)
    reliable = fingerprint is not None and (exclusive is None or exclusive)
    require_checkpoint_project(
        checkpoint,
        node,
        CheckpointProjectObservation(fingerprint=fingerprint, reliable=reliable),
    )


def restore_selected_checkpoints(
    runtime: CompiledRuntimeContext, output: ArtifactStorePort
) -> None:
    """Restore manifest-authorized checkpoints synchronously in compiled node order.

    Validate provenance and dependencies before registering each checkpoint, then
    check the project, register generated captures, and reconstruct progress.
    Publish diagnostics and dependency recovery under the registry lock. The first
    error propagates without rollback; earlier registrations and writes remain.
    Unknown manifest nodes fail after compiled nodes are processed. The caller
    retains ownership of the store, registry, and generated-workspace cleanup.
    """
    summaries = {
        item.node_id: item for item in output.read_hydrated_review_checkpoints()
    }
    for node in runtime.plan.nodes:
        marker = output.read_review_checkpoint(node.id)
        summary = summaries.pop(node.id, None)
        selected = _require_hydration_provenance(runtime, output, marker, summary)
        if selected is not None:
            _restore_checkpoint(runtime, output, node, selected)
    if summaries:
        raise ValueError("Manifest names review checkpoints outside the compiled plan.")


def _require_hydration_provenance(
    runtime: CompiledRuntimeContext,
    output: ArtifactStorePort,
    marker: ReviewLoopCheckpoint | None,
    summary: ReviewCheckpointResumeSummary | None,
) -> OpenReviewCheckpoint | None:
    if summary is None:
        if (
            isinstance(marker, OpenReviewCheckpoint)
            and marker.resume_origin is not None
        ):
            raise ValueError(
                "Review checkpoint marker has no manifest hydration provenance."
            )
        return None
    plan = runtime.plan
    if not isinstance(marker, OpenReviewCheckpoint) or (
        marker.resume_origin != summary.resume_origin
        or (marker.audit, marker.local_round, marker.next_phase)
        != (summary.source_audit, summary.source_local_round, summary.source_phase)
        or marker.run_id != output.run_id
        or marker.run_key_name != output.run_key_name
        or marker.workflow_identity != runtime.workflow_identity
        or marker.workflow_signature != plan.workflow_signature
        or marker.workflow_name != plan.workflow_name
        or marker.plan_schema_version != plan.plan_schema_version
    ):
        raise ValueError("Review checkpoint marker and manifest provenance disagree.")
    return marker


def _restore_checkpoint(
    runtime: CompiledRuntimeContext,
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    checkpoint: OpenReviewCheckpoint,
) -> None:
    require_checkpoint_dependencies(output.stages_dir, runtime.plan, node, checkpoint)
    normal_paths = checkpoint_hydration.verify_workspace_destinations(
        output.stages_dir, checkpoint
    )
    runtime.review_checkpoints[node.id] = checkpoint
    require_entry_project(runtime, output, node)
    _restore_generated_mappings(runtime, output.stages_dir, node.id, checkpoint)
    progress = ProgressRestorer(
        checkpoint,
        output.stages_dir,
        node.provider_records,
        node.execution_policy.audit_rounds or 1,
    ).restore()
    stage_path = node.artifact_contract.stage_path
    if stage_path is None:
        raise ValueError("Review checkpoint node lacks its stage path.")
    _publish_restored_checkpoint(
        runtime, output.stages_dir, stage_path, checkpoint, normal_paths, progress
    )


def _restore_generated_mappings(
    runtime: CompiledRuntimeContext,
    root: Path,
    node_id: str,
    checkpoint: OpenReviewCheckpoint,
) -> None:
    for mapping in checkpoint.generated_mappings:
        path = root / mapping.output_path
        if mapping.snapshot_path is None:
            runtime.generated_file_workspaces.record_capture_failure(node_id, path)
        else:
            runtime.generated_file_workspaces.record(
                node_id, path, root / mapping.snapshot_path
            )


def _publish_restored_checkpoint(
    runtime: CompiledRuntimeContext,
    root: Path,
    stage_path: str,
    checkpoint: OpenReviewCheckpoint,
    normal_paths: list[Path],
    progress: ReviewLoopProgress,
) -> None:
    with runtime.runtime_publications.transaction():
        paths = restore_diagnostics(root / stage_path, checkpoint.node_id, progress)
        for descriptor in checkpoint.files:
            path = root / descriptor.relative_path
            runtime.runtime_publications.publish(
                path, descriptor.signature, recovery_source=path
            )
        marker_path = root / review_checkpoint_relative_path(checkpoint.node_id)
        for path in [*normal_paths, *paths, marker_path]:
            runtime.runtime_publications.publish(
                path, file_size_and_sha256(path), recovery_source=path
            )


def emit_checkpoint_reuse(
    runtime: CompiledRuntimeContext, node_id: str, telemetry: ExecutionTelemetry | None
) -> None:
    """Emit the registered checkpoint cursor, or do nothing when absent.

    Leave checkpoint state unchanged. Missing telemetry or sinks skip delivery;
    sink errors propagate. The caller owns telemetry and the sink's resources.
    """
    checkpoint = runtime.review_checkpoints.get(node_id)
    if checkpoint is not None:
        emit_runtime_log(
            telemetry,
            level=LogLevel.INFO,
            message=f"Node '{node_id}' resumes review checkpoint at audit {checkpoint.audit}, round {checkpoint.local_round}, phase {checkpoint.next_phase}.",
            operation="review_checkpoint_resumed",
            context=RuntimeEventContext(node_id=node_id),
        )
