"""Validate candidate groups, review batches, and checkpoint progress policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from crewplane.core.preflight.models import PreflightExecutionNode
from crewplane.core.review_checkpoint_state import (
    CheckpointCandidate,
    CheckpointProgress,
    CheckpointReview,
    CheckpointReviewerFailure,
)
from crewplane.core.workflow.keywords import ProviderRole

if TYPE_CHECKING:
    from crewplane.core.review_checkpoint import OpenReviewCheckpoint


def validate_checkpoint_progress(
    checkpoint: OpenReviewCheckpoint, node: PreflightExecutionNode
) -> None:
    """Check candidate groups, cursors, settled batches, then review policy.

    Raise ValueError at the first mismatch without mutation or I/O.
    """
    _validate_candidate_groups(checkpoint)
    _validate_progress_cursor(checkpoint, node)
    _validate_selected_reviews(checkpoint)
    _validate_initial_reviews(checkpoint, node)
    _validate_review_policy(checkpoint.progress, node)
    if checkpoint.next_phase == "finalize":
        _validate_finalization(checkpoint, node)


def _validate_candidate_groups(checkpoint: OpenReviewCheckpoint) -> None:
    progress = checkpoint.progress
    active = progress.active_audit
    executors = [
        task.task_id for task in checkpoint.tasks if task.role == ProviderRole.EXECUTOR
    ]
    groups = [progress.latest_executor_outputs]
    if active is not None:
        groups.extend(
            [
                active.executor_outputs,
                active.previous_executor_outputs,
                active.latest_valid_executor_outputs,
            ]
        )
    by_path: dict[str, CheckpointCandidate] = {}
    for group in groups:
        if group is None:
            continue
        _validate_executor_group(group, executors)
        for candidate in group:
            _validate_candidate_identity(candidate, by_path)


def _validate_executor_group(
    group: list[CheckpointCandidate], executors: list[str]
) -> None:
    if [item.task_id for item in group] != executors or len(
        {(item.audit, item.local_round) for item in group}
    ) != 1:
        raise ValueError(
            "Checkpoint candidates require the complete ordered executor phase."
        )


def _validate_candidate_identity(
    candidate: CheckpointCandidate, by_path: dict[str, CheckpointCandidate]
) -> None:
    if candidate.output_path in by_path and by_path[candidate.output_path] != candidate:
        raise ValueError("Checkpoint contains conflicting candidate identities.")
    by_path[candidate.output_path] = candidate
    if (candidate.producer_audit, candidate.producer_round) != (
        candidate.audit,
        candidate.local_round,
    ) and not (
        candidate.audit > candidate.producer_audit and candidate.local_round == 1
    ):
        raise ValueError("Only fresh audits may seed an earlier producer candidate.")


def _validate_progress_cursor(
    checkpoint: OpenReviewCheckpoint, node: PreflightExecutionNode
) -> None:
    progress = checkpoint.progress
    active = progress.active_audit
    if progress.executed_audit_rounds != checkpoint.audit:
        raise ValueError("Checkpoint progress and audit cursor disagree.")
    if checkpoint.next_phase == "executors":
        _validate_executor_cursor(checkpoint)
    for counters in [progress, *([] if active is None else [active])]:
        if (
            max(counters.selected_round_num, counters.last_round_num)
            > (node.execution_policy.depth or 1) + 1
        ):
            raise ValueError("Checkpoint progress exceeds configured round bounds.")
    if (
        checkpoint.next_phase == "reviewers"
        and active is not None
        and any(
            (item.audit, item.local_round) != (checkpoint.audit, checkpoint.local_round)
            for item in active.executor_outputs
        )
    ):
        raise ValueError("Reviewer cursor does not match the active candidate.")


def _validate_selected_reviews(checkpoint: OpenReviewCheckpoint) -> None:
    progress = checkpoint.progress
    active = progress.active_audit
    _validate_selected_batch(
        checkpoint,
        progress.latest_executor_outputs,
        progress.latest_reviewer_outputs,
        progress.reviewer_failures,
        progress.selected_round_num,
    )
    if active is not None:
        _validate_selected_batch(
            checkpoint,
            active.latest_valid_executor_outputs,
            active.latest_reviewer_outputs,
            active.reviewer_failures,
            active.selected_round_num,
        )


def _validate_initial_reviews(
    checkpoint: OpenReviewCheckpoint, node: PreflightExecutionNode
) -> None:
    progress = checkpoint.progress
    if progress.initial_review_completed:
        if node.execution_policy.review_starts_with != "reviewer":
            raise ValueError("Initial review is not configured for this node.")
        _validate_batch(
            checkpoint, progress.initial_reviews, progress.initial_failures, (1, 0)
        )
    elif progress.initial_reviews or progress.initial_failures:
        raise ValueError("Unsettled initial reviews cannot be checkpointed.")


def _validate_review_policy(
    progress: CheckpointProgress, node: PreflightExecutionNode
) -> None:
    if progress.failures() and not node.execution_policy.continue_on_failure:
        raise ValueError("Reviewer failures require continuation policy.")
    if progress.consensus_reached and progress.reviewer_failures:
        raise ValueError("Reviewer failures prevent consensus.")


def _validate_executor_cursor(checkpoint: OpenReviewCheckpoint) -> None:
    active = checkpoint.progress.active_audit
    if checkpoint.local_round == 1:
        if active is not None:
            raise ValueError("Initial executors cannot carry active audit progress.")
        return
    if (
        active is None
        or active.last_round_num != checkpoint.local_round - 1
        or not 1 <= active.selected_round_num < checkpoint.local_round
        or not active.previous_executor_outputs
        or active.previous_executor_outputs != active.latest_valid_executor_outputs
        or active.executor_outputs != active.latest_valid_executor_outputs
        or any(
            item.audit != checkpoint.audit for item in active.previous_executor_outputs
        )
        or active.previous_unresolved_fingerprints
        != tuple(
            sorted(
                {
                    fingerprint
                    for review in active.latest_reviewer_outputs
                    for fingerprint in review.evaluation.unresolved_fingerprints
                }
            )
        )
    ):
        raise ValueError("Remediation cursor disagrees with settled active progress.")


def _validate_finalization(
    checkpoint: OpenReviewCheckpoint, node: PreflightExecutionNode
) -> None:
    progress = checkpoint.progress
    if checkpoint.local_round != progress.last_round_num:
        raise ValueError("Finalization cursor and attempted round disagree.")
    if progress.stop_reason == "consensus":
        permitted = (
            progress.consensus_reached
            and all(
                item.evaluation.approved for item in progress.latest_reviewer_outputs
            )
            and not (
                progress.continued_after_stop or progress.continued_after_exhaustion
            )
        )
    elif progress.stop_reason == "no_progress":
        permitted = (
            node.execution_policy.continue_on_failure
            and progress.continued_after_stop
            and not (progress.consensus_reached or progress.continued_after_exhaustion)
        )
    elif progress.stop_reason == "consensus_exhausted":
        permitted = (
            (
                node.execution_policy.continue_on_failure
                or node.execution_policy.consensus_on_exhaustion == "continue"
            )
            and progress.continued_after_exhaustion
            and progress.continued_after_stop
            and not progress.consensus_reached
        )
    else:
        permitted = False
    if not permitted:
        raise ValueError(
            "Checkpoint finalization is not permitted by the settled review policy."
        )


def _validate_selected_batch(
    checkpoint: OpenReviewCheckpoint,
    candidates: list[CheckpointCandidate] | None,
    reviews: list[CheckpointReview],
    failures: list[CheckpointReviewerFailure],
    selected_round: int,
) -> None:
    if selected_round == 0:
        if reviews or failures:
            raise ValueError("Unselected progress cannot carry a settled review batch.")
        return
    if not candidates or any(item.local_round != selected_round for item in candidates):
        raise ValueError("Selected candidate and round disagree.")
    _validate_batch(
        checkpoint, reviews, failures, (candidates[0].audit, selected_round)
    )


def _validate_batch(
    checkpoint: OpenReviewCheckpoint,
    reviews: list[CheckpointReview],
    failures: list[CheckpointReviewerFailure],
    coordinates: tuple[int, int],
) -> None:
    expected = [
        task.task_id for task in checkpoint.tasks if task.role == ProviderRole.REVIEWER
    ]
    observed = [item.task_id for item in [*reviews, *failures]]
    if sorted(observed) != sorted(expected) or any(
        (item.audit, item.local_round) != coordinates
        or item.role != ProviderRole.REVIEWER
        for item in [*reviews, *failures]
    ):
        raise ValueError("Checkpoint requires a complete settled reviewer batch.")
    failed_task_ids = {item.task_id for item in failures}
    if [item.task_id for item in reviews] != [
        task for task in expected if task not in failed_task_ids
    ]:
        raise ValueError("Checkpoint reviewer order does not match the compiled node.")
