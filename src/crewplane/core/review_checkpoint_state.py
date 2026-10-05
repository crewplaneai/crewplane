"""Neutral executable review progress; paths are relative to the run stage root."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from crewplane.architecture.safe_files import is_safe_relative_path
from crewplane.core.file_hashing import ContentSignature
from crewplane.core.value_checks import is_sha256
from crewplane.core.workflow.keywords import ProviderRole


def validate_checkpoint_path(value: str) -> str:
    if not is_safe_relative_path(value):
        raise ValueError("Checkpoint paths must be normalized relative POSIX paths.")
    return value


def validate_checkpoint_digest(value: str) -> str:
    if not is_sha256(value):
        raise ValueError("Checkpoint digest must be a lowercase SHA-256.")
    return value


type CheckpointPath = Annotated[str, AfterValidator(validate_checkpoint_path)]
type CheckpointDigest = Annotated[str, AfterValidator(validate_checkpoint_digest)]
type CheckpointPhase = Literal["executors", "reviewers", "finalize"]


class CheckpointRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckpointTask(CheckpointRecord):
    task_id: str = Field(min_length=1)
    role: ProviderRole


class CheckpointInvocation(CheckpointTask):
    audit: int = Field(ge=1, strict=True)
    local_round: int = Field(ge=0, strict=True)


class CheckpointFile(CheckpointInvocation):
    purpose: Literal[
        "executor_output",
        "reviewer_output",
        "review_state",
        "review_metadata",
        "review_failure",
        "candidate_identity",
        "generated_file",
        "generated_metadata",
        "workspace_state",
        "workspace_setup",
        "workspace_rendered",
        "workspace_bundle",
    ]
    relative_path: CheckpointPath
    signature: ContentSignature

    @model_validator(mode="after")
    def validate_signature(self) -> CheckpointFile:
        size, digest = self.signature
        if size < 0 or not is_sha256(digest):
            raise ValueError("Invalid checkpoint content signature.")
        return self


class CheckpointCandidateIdentity(CheckpointRecord):
    kind: Literal["document", "files", "unverified"]
    fingerprint: CheckpointDigest | None
    source_fingerprint: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_classification(self) -> CheckpointCandidateIdentity:
        if (self.kind == "unverified") != (self.fingerprint is None):
            raise ValueError("Candidate classification and fingerprint disagree.")
        return self


class CheckpointCandidate(CheckpointInvocation):
    output_path: CheckpointPath
    producer_audit: int = Field(ge=1, strict=True)
    producer_round: int = Field(ge=1, strict=True)
    identity: CheckpointCandidateIdentity


class CheckpointEvaluation(CheckpointRecord):
    verdict: str | None
    approved: bool
    major_issues: str
    minor_issues: str
    nitpicks: str
    unresolved_fingerprints: tuple[str, ...]
    unresolved_issue_count: int = Field(ge=0)
    normalized_markdown: str
    raw_text: str
    evaluation_kind: Literal[
        "structured",
        "plain_language_approval",
        "plain_language_changes_requested",
        "unstructured_feedback",
    ]
    warnings: tuple[str, ...]
    original_verdict: str | None = None
    had_leading_text: bool = False
    had_trailing_text: bool = False
    unstructured_feedback: str | None = None


class CheckpointReview(CheckpointInvocation):
    output_path: CheckpointPath
    evaluation: CheckpointEvaluation


class CheckpointReviewerFailure(CheckpointInvocation):
    failure_kind: str = Field(min_length=1)
    warning: str


class CheckpointStall(CheckpointRecord):
    consecutive_round_count: int = Field(default=0, ge=0)
    candidate_fingerprint: CheckpointDigest | None = None
    review_feedback: tuple[tuple[str, str, str, str | None], ...] = ()


class CheckpointCounters(CheckpointRecord):
    invalid_candidate_round_count: int = Field(default=0, ge=0)
    no_progress_round_count: int = Field(default=0, ge=0)
    artifact_drift_warning_count: int = Field(default=0, ge=0)
    last_round_num: int = Field(default=0, ge=0)
    selected_round_num: int = Field(default=0, ge=0)
    stop_reason: (
        Literal[
            "consensus",
            "consensus_exhausted",
            "no_progress",
            "no_valid_candidate",
            "failed",
            "cancelled",
        ]
        | None
    ) = None


class CheckpointAuditProgress(CheckpointCounters):
    executor_outputs: list[CheckpointCandidate]
    previous_executor_outputs: list[CheckpointCandidate] | None = None
    previous_unresolved_fingerprints: tuple[str, ...] = ()
    previous_executor_fingerprint: CheckpointDigest | None = None
    latest_valid_executor_outputs: list[CheckpointCandidate] | None = None
    latest_reviewer_outputs: list[CheckpointReview] = Field(default_factory=list)
    reviewer_failures: list[CheckpointReviewerFailure] = Field(default_factory=list)


class CheckpointProgress(CheckpointCounters):
    latest_executor_outputs: list[CheckpointCandidate] | None = None
    latest_reviewer_outputs: list[CheckpointReview] = Field(default_factory=list)
    reviewer_failures: list[CheckpointReviewerFailure] = Field(default_factory=list)
    executed_audit_rounds: int = Field(default=0, ge=0)
    consensus_reached: bool = False
    continued_after_exhaustion: bool = False
    continued_after_stop: bool = False
    stall: CheckpointStall = Field(default_factory=CheckpointStall)
    active_audit: CheckpointAuditProgress | None = None
    initial_review_completed: bool = False
    initial_reviews: list[CheckpointReview] = Field(default_factory=list)
    initial_failures: list[CheckpointReviewerFailure] = Field(default_factory=list)

    def candidates(self) -> list[CheckpointCandidate]:
        candidates = list(self.latest_executor_outputs or [])
        if self.active_audit is not None:
            candidates.extend(self.active_audit.executor_outputs)
            candidates.extend(self.active_audit.previous_executor_outputs or [])
            candidates.extend(self.active_audit.latest_valid_executor_outputs or [])
        return candidates

    def reviews(self) -> list[CheckpointReview]:
        reviews = [*self.initial_reviews, *self.latest_reviewer_outputs]
        if self.active_audit is not None:
            reviews.extend(self.active_audit.latest_reviewer_outputs)
        return reviews

    def failures(self) -> list[CheckpointReviewerFailure]:
        failures = [*self.initial_failures, *self.reviewer_failures]
        if self.active_audit is not None:
            failures.extend(self.active_audit.reviewer_failures)
        return failures


class CheckpointGeneratedMapping(CheckpointRecord):
    output_path: CheckpointPath
    snapshot_path: CheckpointPath | None


class CheckpointWorkspace(CheckpointInvocation):
    snapshot_path: CheckpointPath
    destination_path: CheckpointPath


class CheckpointProjectObservation(CheckpointRecord):
    fingerprint: CheckpointDigest | None
    reliable: bool

    @model_validator(mode="after")
    def require_reliable_fingerprint(self) -> CheckpointProjectObservation:
        if self.reliable and self.fingerprint is None:
            raise ValueError("Reliable project observations require a fingerprint.")
        return self
