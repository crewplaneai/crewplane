"""Select dependency-closed resume states and eligible review checkpoints."""

from __future__ import annotations

from crewplane.core.execution_state import (
    NodeState,
)
from crewplane.core.preflight.compiled_records import PreflightExecutionNode
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.review_checkpoint import (
    ClosedReviewCheckpoint,
    OpenReviewCheckpoint,
    validate_checkpoint_node,
)
from crewplane.core.review_checkpoint_state import CheckpointProjectObservation

from ..run_history import RunHistoryRecord
from .checkpoint_store import read_review_checkpoint
from .checkpoint_validation import (
    checkpoint_matches_source,
    require_checkpoint_dependencies,
    require_checkpoint_project,
)


def select_resume_checkpoints(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    dependencies: dict[str, set[str]],
    closed_states: dict[str, NodeState],
    project_observation: CheckpointProjectObservation | None,
    fenced_checkpoint_ids: frozenset[str],
) -> tuple[dict[str, OpenReviewCheckpoint], frozenset[str]]:
    checkpoints: dict[str, OpenReviewCheckpoint] = {}
    closed_ids = set[str]()
    for node in plan.nodes:
        try:
            checkpoint = read_review_checkpoint(source.run_dir, node.id)
            if checkpoint is None or not checkpoint_matches_source(
                checkpoint, source, plan
            ):
                continue
            validate_checkpoint_node(checkpoint, node)
            if isinstance(checkpoint, ClosedReviewCheckpoint):
                closed_ids.add(checkpoint_fence_key(node))
                continue
            if (
                node.id in closed_states
                or checkpoint_fence_key(node) in fenced_checkpoint_ids
                or not dependencies[node.id].issubset(closed_states)
            ):
                continue
            _require_checkpoint_provenance(source, checkpoint)
            require_checkpoint_dependencies(source.run_dir, plan, node, checkpoint)
            require_checkpoint_project(checkpoint, node, project_observation)
        except (ValueError, OSError, RuntimeError):
            continue
        checkpoints[node.id] = checkpoint
    return checkpoints, frozenset(closed_ids)


def _require_checkpoint_provenance(
    source: RunHistoryRecord, checkpoint: OpenReviewCheckpoint
) -> None:
    summary = next(
        (
            item
            for item in source.manifest.resumed_review_checkpoints
            if item.node_id == checkpoint.node_id
        ),
        None,
    )
    origin = None if summary is None else summary.resume_origin
    if checkpoint.resume_origin != origin:
        raise ValueError("Checkpoint marker and manifest hydration origin disagree.")


def checkpoint_fence_key(node: PreflightExecutionNode) -> str:
    provenance = node.expansion
    if provenance is None:
        return node.id
    return f"{provenance.group_id}:{provenance.digest}:{provenance.ordinal}:{node.id}"
