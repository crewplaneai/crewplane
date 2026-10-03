"""Hydrate only dependencies explicitly named by selected checkpoints."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.architecture.ports.artifacts import ArtifactStorePort
from crewplane.architecture.safe_files import (
    contained_regular_file,
    ensure_contained_directory,
)
from crewplane.core.execution_state import ResumeOrigin, ReviewCheckpointResumeSummary
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.review_checkpoint import OpenReviewCheckpoint

from ..atomic import atomic_write_json
from .checkpoint_files import copy_checkpoint_file, read_checkpoint_file
from .checkpoint_store import read_review_checkpoint
from .checkpoint_validation import require_checkpoint_dependencies
from .validation import ValidatedResumeFrontier


def hydrate_review_checkpoints(
    frontier: ValidatedResumeFrontier,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
) -> tuple[str, ...]:
    nodes = {node.id: node for node in plan.nodes}
    for node_id, selected in frontier.checkpoints.items():
        node = nodes[node_id]
        source = read_review_checkpoint(frontier.source.run_dir, node_id)
        if source != selected:
            raise ValueError(
                f"Selected review checkpoint changed for node '{node_id}'."
            )
        require_checkpoint_dependencies(frontier.source.run_dir, plan, node, selected)
        output.create_node_dir(NodeArtifactRequest(node.id, node.artifact_contract))
        for descriptor in selected.files:
            copy_checkpoint_file(frontier.source.run_dir, output.stages_dir, descriptor)
        origin = ResumeOrigin(
            source_run_id=selected.run_id,
            source_run_key_name=selected.run_key_name,
            source_node_id=node_id,
            hydrated_at=datetime.now().isoformat(),
        )
        rewritten = _rewrite_checkpoint(selected, output, origin)
        require_checkpoint_dependencies(output.stages_dir, plan, node, rewritten)
        require_checkpoint_dependencies(frontier.source.run_dir, plan, node, selected)
        if read_review_checkpoint(frontier.source.run_dir, node_id) != selected:
            raise ValueError(
                f"Selected review checkpoint changed while copying '{node_id}'."
            )
        output.write_review_checkpoint(rewritten)
        output.record_hydrated_review_checkpoint(
            ReviewCheckpointResumeSummary(
                node_id=node_id,
                source_audit=selected.audit,
                source_local_round=selected.local_round,
                source_phase=selected.next_phase,
                resume_origin=origin,
            )
        )
    return frontier.checkpoint_node_ids


def _rewrite_checkpoint(
    source: OpenReviewCheckpoint, output: ArtifactStorePort, origin: ResumeOrigin
) -> OpenReviewCheckpoint:
    payload = source.model_dump()
    files = {item.relative_path: item for item in source.files}
    for workspace in source.workspaces:
        descriptor = files[workspace.snapshot_path]
        state = json.loads(read_checkpoint_file(output.stages_dir, descriptor))
        state.update(
            run_id=output.run_id,
            run_key_name=output.run_key_name,
            resume_origin=origin.model_dump(mode="json"),
            updated_at=origin.hydrated_at,
        )
        path = output.stages_dir / workspace.snapshot_path
        atomic_write_json(path, state)
        files[workspace.snapshot_path] = descriptor.model_copy(
            update={"signature": file_size_and_sha256(path)}
        )
        normal = json.loads(json.dumps(state))
        normal["workspace"]["retained_reason"] = "hydrated_resume"
        normal["resume_origin"].update(source_workspace={}, source_execution={})
        destination = output.stages_dir / workspace.destination_path
        ensure_contained_directory(
            output.stages_dir,
            destination.parent.relative_to(output.stages_dir).as_posix(),
        )
        atomic_write_json(destination, normal)
    payload.update(
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        resume_origin=origin,
        files=list(files.values()),
    )
    return OpenReviewCheckpoint.model_validate(payload)


def verify_workspace_destinations(
    root: Path, checkpoint: OpenReviewCheckpoint
) -> list[Path]:
    files = {item.relative_path: item for item in checkpoint.files}
    paths = []
    for workspace in checkpoint.workspaces:
        expected = json.loads(
            read_checkpoint_file(root, files[workspace.snapshot_path])
        )
        expected["workspace"]["retained_reason"] = "hydrated_resume"
        expected["resume_origin"].update(source_workspace={}, source_execution={})
        path = contained_regular_file(root, workspace.destination_path)
        if path is None or json.loads(path.read_bytes()) != expected:
            raise ValueError(
                "Hydrated workspace destination disagrees with checkpoint evidence."
            )
        paths.append(path)
    return paths
