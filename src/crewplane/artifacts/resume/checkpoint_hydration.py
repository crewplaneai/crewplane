"""Hydrate only dependencies explicitly named by selected checkpoints."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import cast

from crewplane.architecture.contracts import JsonObject, JsonValue, NodeArtifactRequest
from crewplane.architecture.ports.artifacts import ArtifactStorePort
from crewplane.architecture.safe_files import (
    contained_regular_file,
    ensure_contained_directory,
)
from crewplane.core.execution_state import ResumeOrigin, ReviewCheckpointResumeSummary
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.core.review_checkpoint_state import CheckpointFile, CheckpointWorkspace

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
    """Copy selected review checkpoints into a fresh filesystem-backed run.

    Args:
        frontier: Validated source selection whose nodes occur in the plan.
        plan: Compatible destination plan; completed upstream dependencies must
            already be hydrated into the destination run.
        output: Destination store with a stage root and a running manifest.

    Returns:
        Selected node IDs in frontier insertion order, or an empty tuple.

    Raises:
        ValueError: A source marker changed, dependencies are invalid, or
            destination publication fails validation.
        OSError: Reading, copying, hashing, or publishing an artifact fails.

    Each node is processed synchronously: validate the source, create its stage
    directory, copy descriptors in file order, then rewrite workspace snapshots
    and publish their destinations in workspace order. Revalidate destination
    dependencies, source dependencies, and the source marker before publishing
    the checkpoint marker, followed by its manifest provenance.

    The first error propagates without rollback. Earlier nodes, copied files,
    rewritten snapshots, and workspace destinations can remain on disk. A
    manifest failure can leave a marker without hydration provenance.
    """
    nodes = {node.id: node for node in plan.nodes}
    for node_id, selected in frontier.checkpoints.items():
        _hydrate_review_checkpoint(
            frontier.source.run_dir, plan, nodes[node_id], selected, output
        )
    return frontier.checkpoint_node_ids


def _hydrate_review_checkpoint(
    source_root: Path,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    selected: OpenReviewCheckpoint,
    output: ArtifactStorePort,
) -> None:
    node_id = node.id
    source = read_review_checkpoint(source_root, node_id)
    if source != selected:
        raise ValueError(f"Selected review checkpoint changed for node '{node_id}'.")
    require_checkpoint_dependencies(source_root, plan, node, selected)
    output.create_node_dir(NodeArtifactRequest(node.id, node.artifact_contract))
    for descriptor in selected.files:
        copy_checkpoint_file(source_root, output.stages_dir, descriptor)
    origin = ResumeOrigin(
        source_run_id=selected.run_id,
        source_run_key_name=selected.run_key_name,
        source_node_id=node_id,
        hydrated_at=datetime.now().isoformat(),
    )
    rewritten = _rewrite_checkpoint(selected, output, origin)
    require_checkpoint_dependencies(output.stages_dir, plan, node, rewritten)
    require_checkpoint_dependencies(source_root, plan, node, selected)
    if read_review_checkpoint(source_root, node_id) != selected:
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


def _rewrite_checkpoint(
    source: OpenReviewCheckpoint, output: ArtifactStorePort, origin: ResumeOrigin
) -> OpenReviewCheckpoint:
    payload = source.model_dump()
    files = {item.relative_path: item for item in source.files}
    for workspace in source.workspaces:
        files[workspace.snapshot_path] = _rewrite_checkpoint_workspace(
            workspace, files[workspace.snapshot_path], output, origin
        )
    payload.update(
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        resume_origin=origin,
        files=list(files.values()),
    )
    return OpenReviewCheckpoint.model_validate(payload)


def _rewrite_checkpoint_workspace(
    workspace: CheckpointWorkspace,
    descriptor: CheckpointFile,
    output: ArtifactStorePort,
    origin: ResumeOrigin,
) -> CheckpointFile:
    state: JsonObject = json.loads(read_checkpoint_file(output.stages_dir, descriptor))
    state.update(
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        resume_origin=origin.model_dump(mode="json"),
        updated_at=origin.hydrated_at,
    )
    path = output.stages_dir / workspace.snapshot_path
    atomic_write_json(path, state)
    rewritten = descriptor.model_copy(update={"signature": file_size_and_sha256(path)})
    normal = _normalized_workspace_destination(state)
    destination = output.stages_dir / workspace.destination_path
    ensure_contained_directory(
        output.stages_dir,
        destination.parent.relative_to(output.stages_dir).as_posix(),
    )
    atomic_write_json(destination, normal)
    return rewritten


def verify_workspace_destinations(
    root: Path, checkpoint: OpenReviewCheckpoint
) -> list[Path]:
    """Verify published workspace state against hydrated checkpoint snapshots.

    Args:
        root: Destination run stage root.
        checkpoint: Hydrated marker with validated dependencies, descriptor-backed
            workspace snapshots, and resume provenance in each snapshot.

    Returns:
        Safe regular destination paths in checkpoint workspace order. An empty
        workspace list returns an empty list. No files are modified.

    Raises:
        ValueError: A snapshot fails descriptor verification, or a destination
            is missing, unsafe, or disagrees with the normalized snapshot.
        json.JSONDecodeError: Snapshot or destination bytes are invalid JSON.
        KeyError: A required descriptor or workspace/provenance field is absent.
        TypeError: Decoded workspace fields do not support the required operations.
        AttributeError: Decoded provenance does not support mapping updates.
        OSError: Inspecting or reading a file fails.

    Snapshot verification and normalization precede each destination read. The
    first error propagates without inspecting later workspaces.
    """
    files = {item.relative_path: item for item in checkpoint.files}
    paths: list[Path] = []
    for workspace in checkpoint.workspaces:
        state: JsonObject = json.loads(
            read_checkpoint_file(root, files[workspace.snapshot_path])
        )
        expected = _normalized_workspace_destination(state)
        path = contained_regular_file(root, workspace.destination_path)
        actual: JsonValue = None if path is None else json.loads(path.read_bytes())
        if path is None or actual != expected:
            raise ValueError(
                "Hydrated workspace destination disagrees with checkpoint evidence."
            )
        paths.append(path)
    return paths


def _normalized_workspace_destination(state: JsonObject) -> JsonObject:
    """Copy snapshot evidence and normalize only destination retention/provenance."""
    normal = deepcopy(state)
    workspace = cast(JsonObject, normal["workspace"])
    workspace["retained_reason"] = "hydrated_resume"
    provenance = cast(JsonObject, normal["resume_origin"])
    provenance.update(source_workspace={}, source_execution={})
    return normal
