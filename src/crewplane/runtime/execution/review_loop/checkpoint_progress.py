"""Encode and restore executable review progress from neutral checkpoint records."""

from __future__ import annotations

from pathlib import Path

from crewplane.artifacts.results.review_loop_status import ReviewLoopStopReason
from crewplane.artifacts.resume.checkpoint_files import read_checkpoint_file
from crewplane.core.preflight.models import ProviderRecord
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.core.review_checkpoint_state import (
    CheckpointAuditProgress,
    CheckpointCandidate,
    CheckpointCandidateIdentity,
    CheckpointCounters,
    CheckpointEvaluation,
    CheckpointProgress,
    CheckpointReview,
    CheckpointStall,
)
from crewplane.core.workflow.keywords import ProviderRole

from ..reviews.types import EvaluatedReviewResult
from .candidate_identity import CandidateIdentity
from .stall import ReviewStallState
from .state import render_unresolved_review_packet
from .types import (
    AuditRoundProgress,
    ExecutorRoundArtifact,
    ReviewerRoundArtifact,
    ReviewLoopProgress,
)


def _counters(progress: AuditRoundProgress | ReviewLoopProgress) -> dict[str, object]:
    return {
        "invalid_candidate_round_count": progress.invalid_candidate_round_count,
        "no_progress_round_count": progress.no_progress_round_count,
        "artifact_drift_warning_count": progress.artifact_drift_warning_count,
        "last_round_num": progress.last_round_num,
        "selected_round_num": progress.selected_round_num,
        "stop_reason": progress.stop_reason,
    }


def _candidate(root: Path, artifact: ExecutorRoundArtifact) -> CheckpointCandidate:
    identity = artifact.candidate_identity
    if identity is None:
        raise ValueError("Checkpoint candidate lacks a bound identity.")
    return CheckpointCandidate(
        task_id=artifact.task_id,
        role=ProviderRole.EXECUTOR,
        audit=artifact.audit_round_num or 1,
        local_round=artifact.round_num,
        producer_audit=artifact.producer_audit or artifact.audit_round_num or 1,
        producer_round=artifact.producer_round or artifact.round_num,
        output_path=artifact.output_file.relative_to(root).as_posix(),
        identity=CheckpointCandidateIdentity(
            kind=identity.kind,
            fingerprint=identity.fingerprint,
            source_fingerprint=identity.source_fingerprint,
            reason=identity.reason,
        ),
    )


def _candidates(
    root: Path, artifacts: list[ExecutorRoundArtifact] | None
) -> list[CheckpointCandidate] | None:
    return (
        None
        if artifacts is None
        else [_candidate(root, artifact) for artifact in artifacts]
    )


def _review(root: Path, artifact: ReviewerRoundArtifact) -> CheckpointReview:
    evaluation = artifact.evaluation
    return CheckpointReview(
        task_id=artifact.task_id,
        role=ProviderRole.REVIEWER,
        audit=artifact.audit_round_num or 1,
        local_round=artifact.round_num,
        output_path=artifact.output_file.relative_to(root).as_posix(),
        evaluation=CheckpointEvaluation.model_validate(
            {
                **evaluation.to_state_dict(),
                "normalized_markdown": evaluation.normalized_markdown,
                "raw_text": evaluation.raw_text,
            }
        ),
    )


def _encode_audit(
    root: Path, active: AuditRoundProgress | None
) -> CheckpointAuditProgress | None:
    if active is None:
        return None
    return CheckpointAuditProgress.model_validate(
        {
            **_counters(active),
            "executor_outputs": _candidates(root, active.executor_outputs),
            "previous_executor_outputs": _candidates(
                root, active.previous_executor_outputs
            ),
            "previous_unresolved_fingerprints": active.previous_unresolved_fingerprints,
            "previous_executor_fingerprint": active.previous_executor_fingerprint,
            "latest_valid_executor_outputs": _candidates(
                root, active.latest_valid_executor_outputs
            ),
            "latest_reviewer_outputs": [
                _review(root, item) for item in active.latest_reviewer_outputs
            ],
            "reviewer_failures": active.reviewer_failures,
        }
    )


def encode_progress(root: Path, progress: ReviewLoopProgress) -> CheckpointProgress:
    """Encode bound runtime outputs relative to the run stage root.

    Candidates must have identities, and all output paths must be under root.
    Encode the active audit before loop and initial-review state, preserving
    list order and absent candidate lists as None. Perform no I/O or mutation;
    checkpoint failure records retain their existing object identities.

    Raises:
        ValueError: A candidate lacks an identity or an output is outside root.
        pydantic.ValidationError: Encoded fields violate the checkpoint schema.
    """
    audit = _encode_audit(root, progress.active_audit)
    return CheckpointProgress.model_validate(
        {
            **_counters(progress),
            "latest_executor_outputs": _candidates(
                root, progress.latest_executor_outputs
            ),
            "latest_reviewer_outputs": [
                _review(root, item) for item in progress.latest_reviewer_outputs
            ],
            "reviewer_failures": progress.reviewer_failures,
            "executed_audit_rounds": progress.executed_audit_rounds,
            "consensus_reached": progress.consensus_reached,
            "continued_after_exhaustion": progress.continued_after_exhaustion,
            "continued_after_stop": progress.continued_after_stop,
            "stall": CheckpointStall(
                consecutive_round_count=progress.stall.consecutive_round_count,
                candidate_fingerprint=progress.stall.candidate_fingerprint,
                review_feedback=progress.stall.review_feedback,
            ),
            "active_audit": audit,
            "initial_review_completed": progress.initial_review_completed,
            "initial_reviews": [
                _review(root, item) for item in progress.initial_reviews
            ],
            "initial_failures": progress.initial_failures,
        }
    )


def _restore_counters(
    source: CheckpointCounters, target: ReviewLoopProgress | AuditRoundProgress
) -> None:
    target.invalid_candidate_round_count = source.invalid_candidate_round_count
    target.no_progress_round_count = source.no_progress_round_count
    target.artifact_drift_warning_count = source.artifact_drift_warning_count
    target.last_round_num = source.last_round_num
    target.selected_round_num = source.selected_round_num
    target.stop_reason = (
        None if source.stop_reason is None else ReviewLoopStopReason(source.stop_reason)
    )


class ProgressRestorer:
    """Restore caller-validated checkpoint state without writing artifacts.

    Callers validate compiled-node policy and checkpoint dependencies first.
    Candidate content is reread with containment and signature checks; reviewer
    evaluations come from stored records without reading reviewer files.
    Restored lists and stall state are new, while providers and failure records
    are reused. The active audit shares its restored loop's stall state.
    """

    def __init__(
        self,
        checkpoint: OpenReviewCheckpoint,
        root: Path,
        providers: list[ProviderRecord],
        audit_rounds: int,
    ) -> None:
        """Index descriptors and the current node's providers without I/O.

        Args:
            checkpoint: Validated open checkpoint for the current compiled node.
            root: Run stage root containing the checkpoint's dependencies.
            providers: Current node providers, indexed by task ID.
            audit_rounds: Configured audit count; counts above one restore audit
                numbers, otherwise runtime artifacts use None.

        Retain checkpoint, provider, and descriptor objects by reference; own
        the lookup dictionaries.
        """
        self.checkpoint = checkpoint
        self.root = root
        self.providers = {provider.task_id: provider for provider in providers}
        self.files = {item.relative_path: item for item in checkpoint.files}
        self.audit_rounds = audit_rounds

    def candidates(
        self, items: list[CheckpointCandidate] | None
    ) -> list[ExecutorRoundArtifact] | None:
        """Restore candidates in list order, preserving None and empty lists.

        Return a new list when present. Propagate the first candidate error
        without reading later candidates or changing the supplied records.
        """
        return None if items is None else [self.candidate(item) for item in items]

    def candidate(self, item: CheckpointCandidate) -> ExecutorRoundArtifact:
        """Render verified bytes into a new artifact with its saved identity.

        Reuse the indexed provider and signature without modifying the file or
        descriptor. Preserve producer coordinates and configured audit numbering.

        Raises:
            KeyError: The output descriptor or task provider is not indexed.
            ValueError: The file is missing, unsafe, or differs from its signature.
            OSError: Inspecting or reading the file fails.
        """
        descriptor = self.files[item.output_path]
        identity = item.identity
        return ExecutorRoundArtifact(
            provider=self.providers[item.task_id],
            task_id=item.task_id,
            content=read_checkpoint_file(self.root, descriptor).decode(
                "utf-8", errors="replace"
            ),
            output_file=self.root / item.output_path,
            audit_round_num=item.audit if self.audit_rounds > 1 else None,
            round_num=item.local_round,
            output_signature=descriptor.signature,
            candidate_identity=CandidateIdentity(
                identity.kind,
                identity.fingerprint,
                identity.source_fingerprint,
                identity.reason,
            ),
            producer_audit=item.producer_audit,
            producer_round=item.producer_round,
        )

    def reviews(self, items: list[CheckpointReview]) -> list[ReviewerRoundArtifact]:
        """Restore ordered review artifacts from saved evaluations without I/O.

        Return a new list and artifacts, reusing indexed providers and descriptor
        signatures. File verification belongs to the caller's dependency check.

        Raises:
            KeyError: A task provider or output descriptor is not indexed.
        """
        return [
            ReviewerRoundArtifact(
                provider=self.providers[item.task_id],
                task_id=item.task_id,
                evaluation=self.evaluation(item.evaluation),
                output_file=self.root / item.output_path,
                audit_round_num=item.audit if self.audit_rounds > 1 else None,
                round_num=item.local_round,
                output_signature=self.files[item.output_path].signature,
            )
            for item in items
        ]

    @staticmethod
    def evaluation(item: CheckpointEvaluation) -> EvaluatedReviewResult:
        """Copy saved evaluation fields into an immutable runtime result without I/O."""
        return EvaluatedReviewResult(
            verdict=item.verdict,
            approved=item.approved,
            major_issues=item.major_issues,
            minor_issues=item.minor_issues,
            nitpicks=item.nitpicks,
            unresolved_fingerprints=item.unresolved_fingerprints,
            unresolved_issue_count=item.unresolved_issue_count,
            normalized_markdown=item.normalized_markdown,
            raw_text=item.raw_text,
            evaluation_kind=item.evaluation_kind,
            warnings=item.warnings,
            original_verdict=item.original_verdict,
            had_leading_text=item.had_leading_text,
            had_trailing_text=item.had_trailing_text,
            unstructured_feedback=item.unstructured_feedback,
        )

    def restore(self) -> ReviewLoopProgress:
        """Return fresh loop progress and an optional audit sharing its stall state.

        Restore loop outputs and initial reviews before the active audit. Rebuild
        audit feedback from saved reviews only when previous outputs are present,
        including an empty list. Preserve optional candidate lists and restore
        counters, stop reasons, and the checkpoint cursor without mutation.

        Propagate candidate read and lookup errors, and review lookup errors,
        stopping before later conversions. No partial progress is returned.
        """
        stored = self.checkpoint.progress
        progress = ReviewLoopProgress(
            latest_executor_outputs=self.candidates(stored.latest_executor_outputs),
            latest_reviewer_outputs=self.reviews(stored.latest_reviewer_outputs),
            reviewer_failures=list(stored.reviewer_failures),
            executed_audit_rounds=stored.executed_audit_rounds,
            consensus_reached=stored.consensus_reached,
            continued_after_exhaustion=stored.continued_after_exhaustion,
            continued_after_stop=stored.continued_after_stop,
            stall=ReviewStallState(
                stored.stall.consecutive_round_count,
                stored.stall.candidate_fingerprint,
                stored.stall.review_feedback,
            ),
            initial_review_completed=stored.initial_review_completed,
            initial_reviews=self.reviews(stored.initial_reviews),
            initial_failures=list(stored.initial_failures),
            cursor_audit=self.checkpoint.audit,
            cursor_round=self.checkpoint.local_round,
            next_phase=self.checkpoint.next_phase,
        )
        _restore_counters(stored, progress)
        if stored.active_audit is not None:
            progress.active_audit = self._restore_audit(
                stored.active_audit, progress.stall
            )
        return progress

    def _restore_audit(
        self, active: CheckpointAuditProgress, stall: ReviewStallState
    ) -> AuditRoundProgress:
        reviews = self.reviews(active.latest_reviewer_outputs)
        audit = AuditRoundProgress(
            executor_outputs=self.candidates(active.executor_outputs) or [],
            previous_executor_outputs=self.candidates(active.previous_executor_outputs),
            previous_review_packet=render_unresolved_review_packet(reviews)
            if active.previous_executor_outputs is not None
            else None,
            previous_unresolved_fingerprints=active.previous_unresolved_fingerprints,
            previous_executor_fingerprint=active.previous_executor_fingerprint,
            latest_valid_executor_outputs=self.candidates(
                active.latest_valid_executor_outputs
            ),
            latest_reviewer_outputs=reviews,
            reviewer_failures=list(active.reviewer_failures),
            stall=stall,
        )
        _restore_counters(active, audit)
        return audit
