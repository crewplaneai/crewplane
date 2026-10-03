"""Strict records for reuse between complete review-loop phases."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from crewplane.architecture.contracts.artifacts import (
    build_review_audit_directory_name,
    build_task_round_filename,
)
from crewplane.core.execution_state import (
    ResumeOrigin,
    validate_run_state_schema_version,
)
from crewplane.core.preflight.models import PreflightExecutionNode
from crewplane.core.preflight.plan_contract import (
    validate_supported_plan_schema_version,
)
from crewplane.core.review_checkpoint_state import (
    CheckpointCandidate,
    CheckpointDigest,
    CheckpointFile,
    CheckpointGeneratedMapping,
    CheckpointPhase,
    CheckpointProgress,
    CheckpointProjectObservation,
    CheckpointRecord,
    CheckpointReview,
    CheckpointReviewerFailure,
    CheckpointTask,
    CheckpointWorkspace,
)
from crewplane.core.workflow.keywords import (
    ProviderRole,
    provider_role_segments_are_contiguous,
)


class CheckpointIdentity(CheckpointRecord):
    run_state_schema_version: int
    plan_schema_version: str
    workflow_identity: str = Field(min_length=1)
    workflow_name: str = Field(min_length=1)
    workflow_signature: CheckpointDigest
    run_id: str = Field(min_length=1)
    run_key_name: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    tasks: list[CheckpointTask]

    @field_validator("run_state_schema_version")
    @classmethod
    def validate_state_version(cls, value: int) -> int:
        return validate_run_state_schema_version(value)

    @field_validator("plan_schema_version")
    @classmethod
    def validate_plan_version(cls, value: str) -> str:
        return validate_supported_plan_schema_version(value)


class OpenReviewCheckpoint(CheckpointIdentity):
    kind: Literal["open"] = "open"
    audit: int = Field(ge=1, strict=True)
    local_round: int = Field(ge=0, strict=True)
    next_phase: CheckpointPhase
    progress: CheckpointProgress
    files: list[CheckpointFile]
    generated_mappings: list[CheckpointGeneratedMapping] = Field(default_factory=list)
    workspaces: list[CheckpointWorkspace] = Field(default_factory=list)
    project_observation: CheckpointProjectObservation | None = None
    resume_origin: ResumeOrigin | None = None

    @model_validator(mode="after")
    def validate_references(self) -> OpenReviewCheckpoint:
        by_path = {item.relative_path: item for item in self.files}
        if len(by_path) != len(self.files):
            raise ValueError("Checkpoint file paths must be unique.")
        for candidate in self.progress.candidates():
            descriptor = by_path.get(candidate.output_path)
            if descriptor is None or (
                descriptor.purpose != "executor_output"
                or descriptor.task_id != candidate.task_id
                or descriptor.role != ProviderRole.EXECUTOR
                or descriptor.audit != candidate.producer_audit
                or descriptor.local_round != candidate.producer_round
                or candidate.role != ProviderRole.EXECUTOR
                or (candidate.producer_audit, candidate.producer_round)
                > (candidate.audit, candidate.local_round)
            ):
                raise ValueError("Candidate lacks matching producer evidence.")
        for review in self.progress.reviews():
            descriptor = by_path.get(review.output_path)
            if descriptor is None or (
                descriptor.purpose != "reviewer_output"
                or descriptor.task_id != review.task_id
                or descriptor.role != ProviderRole.REVIEWER
                or descriptor.audit != review.audit
                or descriptor.local_round != review.local_round
                or review.role != ProviderRole.REVIEWER
            ):
                raise ValueError("Review lacks matching invocation evidence.")
        outputs = {item.output_path for item in self.progress.candidates()}
        outputs.update(item.output_path for item in self.progress.reviews())
        mappings = [item.output_path for item in self.generated_mappings]
        if len(mappings) != len(set(mappings)) or not set(mappings) <= outputs:
            raise ValueError(
                "Generated mappings must uniquely reference carried outputs."
            )
        destinations = [item.destination_path for item in self.workspaces]
        if len(destinations) != len(set(destinations)):
            raise ValueError("Workspace destinations must be unique.")
        for workspace in self.workspaces:
            descriptor = by_path.get(workspace.snapshot_path)
            if (
                descriptor is None
                or descriptor.purpose != "workspace_state"
                or (
                    descriptor.task_id,
                    descriptor.role,
                    descriptor.audit,
                    descriptor.local_round,
                )
                != (
                    workspace.task_id,
                    workspace.role,
                    workspace.audit,
                    workspace.local_round,
                )
            ):
                raise ValueError(
                    "Workspace snapshot lacks matching invocation evidence."
                )
        if self.next_phase == "finalize" and (
            self.progress.active_audit is not None
            or not self.progress.latest_executor_outputs
            or self.progress.selected_round_num < 1
        ):
            raise ValueError("Finalization requires a settled selected candidate.")
        if (
            self.next_phase == "reviewers"
            and self.local_round > 0
            and (
                self.progress.active_audit is None
                or not self.progress.active_audit.executor_outputs
            )
        ):
            raise ValueError("Reviewers require a complete active candidate.")
        if (
            self.next_phase == "executors"
            and self.local_round > 1
            and self.progress.active_audit is None
        ):
            raise ValueError("Remediation executors require active audit progress.")
        return self


class ClosedReviewCheckpoint(CheckpointIdentity):
    kind: Literal["closed"] = "closed"
    terminal_reason: str = Field(min_length=1)


type ReviewLoopCheckpoint = Annotated[
    OpenReviewCheckpoint | ClosedReviewCheckpoint, Field(discriminator="kind")
]

REVIEW_CHECKPOINT_ADAPTER: TypeAdapter[ReviewLoopCheckpoint] = TypeAdapter(
    ReviewLoopCheckpoint
)


def checkpoint_node_is_eligible(node: PreflightExecutionNode) -> bool:
    roles = [provider.role for provider in node.provider_records]
    return (
        node.mode == "sequential"
        and ProviderRole.EXECUTOR in roles
        and ProviderRole.REVIEWER in roles
        and provider_role_segments_are_contiguous(roles)
    )


def validate_checkpoint_node(
    checkpoint: ReviewLoopCheckpoint, node: PreflightExecutionNode
) -> None:
    tasks = [
        CheckpointTask(task_id=p.task_id, role=p.role) for p in node.provider_records
    ]
    if (
        not checkpoint_node_is_eligible(node)
        or checkpoint.node_id != node.id
        or checkpoint.tasks != tasks
    ):
        raise ValueError("Checkpoint node identity or provider topology mismatch.")
    if isinstance(checkpoint, ClosedReviewCheckpoint):
        return
    _validate_progress(checkpoint, node)
    audit_rounds = node.execution_policy.audit_rounds or 1
    max_round = (node.execution_policy.depth or 1) + 1
    if checkpoint.audit > audit_rounds or checkpoint.local_round > max_round:
        raise ValueError("Checkpoint cursor exceeds configured review bounds.")
    if checkpoint.local_round == 0 and not (
        checkpoint.audit == 1
        and checkpoint.next_phase == "reviewers"
        and node.execution_policy.review_starts_with == "reviewer"
    ):
        raise ValueError("Round zero is only valid for initial pre-review.")
    identities = {item.task_id: item.role for item in tasks}
    records = [
        *checkpoint.files,
        *checkpoint.progress.candidates(),
        *checkpoint.progress.reviews(),
        *checkpoint.progress.failures(),
        *checkpoint.workspaces,
    ]
    for item in records:
        if (
            identities.get(item.task_id) != item.role
            or item.audit > checkpoint.audit
            or item.local_round > max_round
            or (item.audit, item.local_round)
            > (checkpoint.audit, checkpoint.local_round)
        ):
            raise ValueError("Checkpoint invocation identity or coordinates mismatch.")
        if item.local_round == 0 and not (
            item.audit == 1
            and item.role == ProviderRole.REVIEWER
            and node.execution_policy.review_starts_with == "reviewer"
        ):
            raise ValueError("Invalid round-zero invocation.")
    stage = node.artifact_contract.stage_path
    if stage is None or any(
        not item.relative_path.startswith(stage + "/") for item in checkpoint.files
    ):
        raise ValueError(
            "Checkpoint dependencies must belong to the compiled node stage."
        )
    outputs: list[CheckpointCandidate | CheckpointReview] = [
        *checkpoint.progress.candidates(),
        *checkpoint.progress.reviews(),
    ]
    for output in outputs:
        prefix = stage + "/"
        if audit_rounds > 1:
            prefix += build_review_audit_directory_name(output.audit) + "/"
        expected = prefix + build_task_round_filename(
            output.task_id, output.local_round
        )
        if output.output_path != expected:
            raise ValueError(
                "Checkpoint output path does not match its selected coordinates."
            )


def _validate_progress(
    checkpoint: OpenReviewCheckpoint, node: PreflightExecutionNode
) -> None:
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
        if [item.task_id for item in group] != executors or len(
            {(item.audit, item.local_round) for item in group}
        ) != 1:
            raise ValueError(
                "Checkpoint candidates require the complete ordered executor phase."
            )
        for candidate in group:
            if (
                candidate.output_path in by_path
                and by_path[candidate.output_path] != candidate
            ):
                raise ValueError(
                    "Checkpoint contains conflicting candidate identities."
                )
            by_path[candidate.output_path] = candidate
            if (candidate.producer_audit, candidate.producer_round) != (
                candidate.audit,
                candidate.local_round,
            ) and not (
                candidate.audit > candidate.producer_audit
                and candidate.local_round == 1
            ):
                raise ValueError(
                    "Only fresh audits may seed an earlier producer candidate."
                )
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
    if progress.initial_review_completed:
        if node.execution_policy.review_starts_with != "reviewer":
            raise ValueError("Initial review is not configured for this node.")
        _validate_batch(
            checkpoint, progress.initial_reviews, progress.initial_failures, (1, 0)
        )
    elif progress.initial_reviews or progress.initial_failures:
        raise ValueError("Unsettled initial reviews cannot be checkpointed.")
    if progress.failures() and not node.execution_policy.continue_on_failure:
        raise ValueError("Reviewer failures require continuation policy.")
    if progress.consensus_reached and progress.reviewer_failures:
        raise ValueError("Reviewer failures prevent consensus.")
    if checkpoint.next_phase == "finalize":
        _validate_finalization(checkpoint, node)


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
    if [item.task_id for item in reviews] != [
        task for task in expected if task not in {item.task_id for item in failures}
    ]:
        raise ValueError("Checkpoint reviewer order does not match the compiled node.")
