"""Validate checkpoint identity and its complete descriptor-backed dependencies."""

from __future__ import annotations

import json
from pathlib import Path

from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from crewplane.core.review_checkpoint import (
    OpenReviewCheckpoint,
    ReviewLoopCheckpoint,
    validate_checkpoint_node,
)
from crewplane.core.review_checkpoint_state import (
    CheckpointFile,
    CheckpointProjectObservation,
    CheckpointReview,
    CheckpointReviewerFailure,
)

from ..run_history import RunHistoryRecord
from ..workspace.checkpoint_state import validate_checkpoint_workspaces
from .checkpoint_files import (
    describe_review_evidence,
    read_checkpoint_file,
    verify_checkpoint_files,
)
from .checkpoint_generated_files import validate_generated_mappings


def checkpoint_matches_source(
    checkpoint: ReviewLoopCheckpoint,
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
) -> bool:
    """Return whether the checkpoint belongs to the source run and compiled plan.

    Compare source run ID, run key, and workflow identity before the plan's
    workflow name, signature, and schema version. Return False at the first
    mismatch, or True when all match. This performs no I/O or dependency checks.
    """
    return (
        checkpoint.run_id == source.manifest.run_id
        and checkpoint.run_key_name == source.manifest.run_key_name
        and checkpoint.workflow_identity == source.manifest.workflow_identity
        and checkpoint.workflow_name == plan.workflow_name
        and checkpoint.workflow_signature == plan.workflow_signature
        and checkpoint.plan_schema_version == plan.plan_schema_version
    )


def require_checkpoint_dependencies(
    root: Path,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    checkpoint: OpenReviewCheckpoint,
) -> None:
    """Require valid progress and exactly the descriptor-backed dependencies.

    Check node validity, files in descriptor order, generated mappings, then
    workspaces. Review evidence checks follow: descriptors, candidate identities,
    each review's published then raw text, and review/failure states in evidence
    order. Finally reject unbound files. Stop at the first error without writing
    files or modifying checkpoint data; validation runs synchronously.

    Returns:
        None when all dependencies are valid and bound.

    Raises:
        ValueError: Progress, dependencies, or evidence are invalid. JSON parsing
            and UTF-8 decoding failures propagate unchanged.
        OSError: Inspecting, hashing, or reading a dependency fails.
        RuntimeError: Workspace validation fails.
    """
    validate_checkpoint_node(checkpoint, node)
    verify_checkpoint_files(root, checkpoint.files)
    generated = validate_generated_mappings(
        root, checkpoint.generated_mappings, checkpoint.files
    )
    workspace = validate_checkpoint_workspaces(root, plan, node, checkpoint)
    evidence = _validate_review_evidence(root, checkpoint, node)
    outputs = {item.output_path for item in checkpoint.progress.candidates()}
    outputs.update(item.output_path for item in checkpoint.progress.reviews())
    if {
        item.relative_path for item in checkpoint.files
    } != outputs | evidence | generated | workspace:
        raise ValueError("Checkpoint contains unbound file dependencies.")


def _validate_review_evidence(
    root: Path, checkpoint: OpenReviewCheckpoint, node: PreflightExecutionNode
) -> set[str]:
    descriptors = {item.relative_path: item for item in checkpoint.files}
    expected = describe_review_evidence(
        root,
        checkpoint.progress,
        node.artifact_contract.stage_path or "",
        node.execution_policy.audit_rounds or 1,
    )
    _require_matching_evidence_descriptors(descriptors, expected)
    _validate_candidate_identities(root, checkpoint, descriptors)
    _validate_review_texts(root, checkpoint, descriptors)
    _validate_review_states(root, checkpoint, node, expected)
    return {item.relative_path for item in expected}


def _require_matching_evidence_descriptors(
    descriptors: dict[str, CheckpointFile], expected: list[CheckpointFile]
) -> None:
    if any(descriptors.get(item.relative_path) != item for item in expected):
        raise ValueError("Checkpoint review evidence lacks matching descriptors.")


def _validate_candidate_identities(
    root: Path,
    checkpoint: OpenReviewCheckpoint,
    descriptors: dict[str, CheckpointFile],
) -> None:
    for candidate in checkpoint.progress.candidates():
        path = Path(candidate.output_path).with_suffix(".candidate.json").as_posix()
        if json.loads(
            read_checkpoint_file(root, descriptors[path])
        ) != candidate.identity.model_dump(mode="json"):
            raise ValueError(
                "Checkpoint candidate identity disagrees with its evidence."
            )


def _validate_review_texts(
    root: Path,
    checkpoint: OpenReviewCheckpoint,
    descriptors: dict[str, CheckpointFile],
) -> None:
    for review in checkpoint.progress.reviews():
        if (
            read_checkpoint_file(root, descriptors[review.output_path]).decode("utf-8")
            != review.evaluation.normalized_markdown
        ):
            raise ValueError(
                "Checkpoint evaluation disagrees with its published review."
            )
        raw = Path(review.output_path).with_suffix(".raw.txt").as_posix()
        if (
            read_checkpoint_file(root, descriptors[raw]).decode("utf-8")
            != review.evaluation.raw_text
        ):
            raise ValueError(
                "Checkpoint evaluation disagrees with its original review."
            )


def _validate_review_states(
    root: Path,
    checkpoint: OpenReviewCheckpoint,
    node: PreflightExecutionNode,
    expected: list[CheckpointFile],
) -> None:
    settled: list[CheckpointReview | CheckpointReviewerFailure] = [
        *checkpoint.progress.reviews(),
        *checkpoint.progress.failures(),
    ]
    records = {(item.task_id, item.audit, item.local_round): item for item in settled}
    for descriptor in expected:
        if descriptor.purpose not in {"review_state", "review_failure"}:
            continue
        record = records[(descriptor.task_id, descriptor.audit, descriptor.local_round)]
        state = json.loads(read_checkpoint_file(root, descriptor))
        if not isinstance(state, dict) or any(
            state.get(key) != value
            for key, value in _review_state_fields(record, node).items()
        ):
            raise ValueError(
                "Checkpoint review state disagrees with its evaluation or failure."
            )


def _review_state_fields(
    record: CheckpointReview | CheckpointReviewerFailure, node: PreflightExecutionNode
) -> dict[str, object]:
    fields: dict[str, object] = {
        "task_id": record.task_id,
        "audit_round_num": record.audit
        if (node.execution_policy.audit_rounds or 1) > 1
        else None,
        "round_num": record.local_round,
    }
    if isinstance(record, CheckpointReview):
        fields.update(
            record.evaluation.model_dump(
                mode="json", exclude={"normalized_markdown", "raw_text"}
            )
        )
    else:
        fields.update(
            approved=False,
            evaluation_kind="reviewer_failure",
            failure_kind=record.failure_kind,
            warnings=[record.warning],
        )
    return fields


def require_checkpoint_project(
    checkpoint: OpenReviewCheckpoint,
    node: PreflightExecutionNode,
    observation: CheckpointProjectObservation | None,
) -> None:
    """Require the project observation appropriate to the node's workspace policy.

    Enabled managed workspaces require an absent saved project observation and
    ignore the supplied observation. Otherwise check the saved observation's
    presence and reliability, then the supplied observation's, then fingerprint
    equality. Neither observation nor checkpoint data is modified.

    Returns:
        None when the applicable observation requirements hold.

    Raises:
        ValueError: A managed checkpoint carries a project observation, or an
            unmanaged project's contents changed or cannot be verified.
    """
    policy = node.workspace_policy
    if policy is not None and policy.enabled:
        if checkpoint.project_observation is not None:
            raise ValueError(
                "Managed checkpoint cannot carry an unmanaged project observation."
            )
        return
    expected = checkpoint.project_observation
    if (
        expected is None
        or not expected.reliable
        or observation is None
        or not observation.reliable
        or observation.fingerprint != expected.fingerprint
    ):
        raise ValueError(
            f"Project contents changed or cannot be verified for review checkpoint '{node.id}'."
        )
