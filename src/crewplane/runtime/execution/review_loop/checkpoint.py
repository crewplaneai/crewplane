"""Publish phase checkpoints and restore runtime registrations and diagnostics."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from crewplane.architecture.contracts import LogLevel
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.naming import review_checkpoint_relative_path
from crewplane.artifacts.results.findings import (
    FindingsSelection,
    extract_findings_content,
)
from crewplane.artifacts.resume.checkpoint_files import (
    describe_checkpoint_file,
    describe_review_evidence,
)
from crewplane.artifacts.resume.checkpoint_generated_files import (
    describe_generated_mapping,
)
from crewplane.artifacts.resume.checkpoint_validation import (
    require_checkpoint_dependencies,
    require_checkpoint_project,
)
from crewplane.core.execution_state import RUN_STATE_SCHEMA_VERSION
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
    CheckpointCandidate,
    CheckpointFile,
    CheckpointInvocation,
    CheckpointPhase,
    CheckpointProgress,
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

if TYPE_CHECKING:
    from crewplane.artifacts.workspace.checkpoint_state import (
        PreparedCheckpointWorkspaces,
    )


def checkpoint_identity(context: ReviewLoopRunContext) -> CheckpointIdentity:
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
    policy = context.stage.workspace_policy
    if policy is not None and policy.enabled:
        return None
    telemetry = context.telemetry
    tracker = None if telemetry is None else telemetry.activity_tracker
    before = None if tracker is None else tracker.snapshot(context.stage.id)
    fingerprint = project_fingerprint(
        Path(context.runtime_context.plan.project_root),
        (context.output.stages_dir.parent.parent,),
    )
    after = None if tracker is None else tracker.snapshot(context.stage.id)
    exclusive = (
        before.is_exclusive and before == after
        if before is not None
        else (
            context.runtime_context.max_concurrent_nodes() == 1
            or len(context.runtime_context.plan.execution_order) == 1
        )
    )
    return CheckpointProjectObservation(
        fingerprint=fingerprint, reliable=fingerprint is not None and exclusive
    )


def _output_dependencies(
    context: ReviewLoopRunContext, progress: CheckpointProgress
) -> list[CheckpointFile]:
    root = context.output.stages_dir
    artifacts = progress.candidates() + progress.reviews()
    files: dict[str, CheckpointFile] = {}
    for item in artifacts:
        is_candidate = isinstance(item, CheckpointCandidate)
        invocation = CheckpointInvocation(
            task_id=item.task_id,
            role=item.role,
            audit=item.producer_audit
            if isinstance(item, CheckpointCandidate)
            else item.audit,
            local_round=item.producer_round
            if isinstance(item, CheckpointCandidate)
            else item.local_round,
        )
        path = root / item.output_path
        signature = context.runtime_context.runtime_publications.snapshot()[0].get(path)
        if signature is None:
            raise ValueError(f"Checkpoint output has no runtime publication: {path}")
        descriptor = describe_checkpoint_file(
            root,
            item.output_path,
            invocation,
            "executor_output" if is_candidate else "reviewer_output",
            signature,
        )
        files[item.output_path] = descriptor
    return list(files.values())


def _prepare_checkpoint(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
    phase: CheckpointPhase,
    local_round: int,
) -> tuple[OpenReviewCheckpoint, PreparedCheckpointWorkspaces]:
    from crewplane.artifacts.workspace.checkpoint_state import (
        prepare_checkpoint_workspaces,
    )

    root = context.output.stages_dir
    stored = encode_progress(root, progress)
    files = _output_dependencies(context, stored)
    files.extend(
        describe_review_evidence(
            root,
            stored,
            context.node_dir.relative_to(root).as_posix(),
            context.audit_rounds,
        )
    )
    mappings = []
    roots = context.runtime_context.generated_file_workspaces.roots_for_node(
        context.stage.id
    )
    for descriptor in list(files):
        if descriptor.purpose not in {"executor_output", "reviewer_output"}:
            continue
        path = (root / descriptor.relative_path).resolve()
        if path in roots:
            invocation = CheckpointInvocation(
                task_id=descriptor.task_id,
                role=descriptor.role,
                audit=descriptor.audit,
                local_round=descriptor.local_round,
            )
            mapping, dependencies = describe_generated_mapping(
                root, descriptor.relative_path, roots[path], invocation
            )
            mappings.append(mapping)
            files.extend(dependencies)
    workspace = prepare_checkpoint_workspaces(
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
    from crewplane.artifacts.workspace.checkpoint_state import (
        publish_checkpoint_workspaces,
    )

    checkpoint, workspace = await asyncio.to_thread(
        _prepare_checkpoint, context, progress, phase, local_round
    )
    root = context.output.stages_dir
    publications = context.runtime_context.runtime_publications
    with publications.transaction():
        publish_checkpoint_workspaces(root, workspace)
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
    selection = FindingsSelection.from_stage(
        build_stage_task_specs(context.stage), context.stage.findings
    )
    for artifact in progress.latest_executor_outputs or []:
        if selection.should_extract(artifact.task_id, artifact.content):
            extract_findings_content(artifact.content, artifact.output_file)


def restore_diagnostics(
    node_dir: Path, node_id: str, progress: ReviewLoopProgress
) -> list[Path]:
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
    checkpoint = runtime.review_checkpoints.get(node.id)
    if checkpoint is None:
        return
    policy = node.workspace_policy
    if policy is not None and policy.enabled:
        return
    tracker = None if telemetry is None else telemetry.activity_tracker
    before = None if tracker is None else tracker.snapshot(node.id)
    fingerprint = project_fingerprint(
        Path(runtime.plan.project_root), (output.stages_dir.parent.parent,)
    )
    after = None if tracker is None else tracker.snapshot(node.id)
    reliable = fingerprint is not None and (
        before is None or (before.is_exclusive and before == after)
    )
    require_checkpoint_project(
        checkpoint,
        node,
        CheckpointProjectObservation(fingerprint=fingerprint, reliable=reliable),
    )


def restore_selected_checkpoints(
    runtime: CompiledRuntimeContext, output: ArtifactStorePort
) -> None:
    from crewplane.artifacts.resume.checkpoint_hydration import (
        verify_workspace_destinations,
    )

    summaries = {
        item.node_id: item for item in output.read_hydrated_review_checkpoints()
    }
    plan = runtime.plan
    for node in plan.nodes:
        marker = output.read_review_checkpoint(node.id)
        summary = summaries.pop(node.id, None)
        if summary is None:
            if (
                isinstance(marker, OpenReviewCheckpoint)
                and marker.resume_origin is not None
            ):
                raise ValueError(
                    "Review checkpoint marker has no manifest hydration provenance."
                )
            continue
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
            raise ValueError(
                "Review checkpoint marker and manifest provenance disagree."
            )
        require_checkpoint_dependencies(output.stages_dir, plan, node, marker)
        normal_paths = verify_workspace_destinations(output.stages_dir, marker)
        runtime.review_checkpoints[node.id] = marker
        require_entry_project(runtime, output, node)
        for mapping in marker.generated_mappings:
            path = output.stages_dir / mapping.output_path
            if mapping.snapshot_path is None:
                runtime.generated_file_workspaces.record_capture_failure(node.id, path)
            else:
                runtime.generated_file_workspaces.record(
                    node.id, path, output.stages_dir / mapping.snapshot_path
                )
        progress = ProgressRestorer(
            marker,
            output.stages_dir,
            node.provider_records,
            node.execution_policy.audit_rounds or 1,
        ).restore()
        stage_path = node.artifact_contract.stage_path
        if stage_path is None:
            raise ValueError("Review checkpoint node lacks its stage path.")
        with runtime.runtime_publications.transaction():
            paths = restore_diagnostics(
                output.stages_dir / stage_path, node.id, progress
            )
            for descriptor in marker.files:
                path = output.stages_dir / descriptor.relative_path
                runtime.runtime_publications.publish(
                    path, descriptor.signature, recovery_source=path
                )
            marker_path = output.stages_dir / review_checkpoint_relative_path(node.id)
            for path in [*normal_paths, *paths, marker_path]:
                runtime.runtime_publications.publish(
                    path, file_size_and_sha256(path), recovery_source=path
                )
    if summaries:
        raise ValueError("Manifest names review checkpoints outside the compiled plan.")


def emit_checkpoint_reuse(
    runtime: CompiledRuntimeContext, node_id: str, telemetry: ExecutionTelemetry | None
) -> None:
    checkpoint = runtime.review_checkpoints.get(node_id)
    if checkpoint is not None:
        emit_runtime_log(
            telemetry,
            level=LogLevel.INFO,
            message=f"Node '{node_id}' resumes review checkpoint at audit {checkpoint.audit}, round {checkpoint.local_round}, phase {checkpoint.next_phase}.",
            operation="review_checkpoint_resumed",
            context=RuntimeEventContext(node_id=node_id),
        )
