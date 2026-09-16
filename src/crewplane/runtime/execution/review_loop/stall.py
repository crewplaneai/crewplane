from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import ReviewerRoundArtifact

MAX_CONSECUTIVE_NO_PROGRESS_ROUNDS = 2


@dataclass
class ReviewStallState:
    consecutive_round_count: int = 0
    candidate_fingerprint: str | None = None
    review_feedback: tuple[tuple[str, str, str, str | None], ...] = ()

    def observe_review(
        self, fingerprint: str | None, reviews: list[ReviewerRoundArtifact]
    ) -> None:
        feedback = tuple(
            (
                review.task_id,
                review.evaluation.major_issues,
                review.evaluation.minor_issues,
                review.evaluation.unstructured_feedback,
            )
            for review in reviews
        )
        if (
            fingerprint is None
            or fingerprint != self.candidate_fingerprint
            or feedback != self.review_feedback
        ):
            self.consecutive_round_count = 0
        self.candidate_fingerprint = fingerprint
        self.review_feedback = feedback

    def record_unchanged_attempt(self) -> bool:
        self.consecutive_round_count += 1
        return self.consecutive_round_count >= MAX_CONSECUTIVE_NO_PROGRESS_ROUNDS
