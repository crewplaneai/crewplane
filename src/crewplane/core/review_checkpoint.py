"""Strict records for reuse between complete review-loop phases."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, field_validator, model_validator

from crewplane.architecture.contracts.artifacts import (
    build_review_audit_directory_name,
    build_task_round_filename,
)
from crewplane.core import review_checkpoint_progress
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
    """Versioned workflow, run, and ordered provider identity shared by markers."""

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
        """Return the supported version or raise ValueError, without I/O."""
        return validate_run_state_schema_version(value)

    @field_validator("plan_schema_version")
    @classmethod
    def validate_plan_version(cls, value: str) -> str:
        """Return the supported version or raise ValueError, without I/O."""
        return validate_supported_plan_schema_version(value)


class OpenReviewCheckpoint(CheckpointIdentity):
    """Resumable phase progress with descriptor-backed outputs and workspaces.

    Construction validates references in memory; compiled-node policy and
    filesystem evidence require separate validation.
    """

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
    def validate_references(self) -> Self:
        """Return self after checking evidence, mappings, workspaces, then phase.

        Raise ValueError at the first mismatch without mutation or I/O.
        """
        by_path = {item.relative_path: item for item in self.files}
        if len(by_path) != len(self.files):
            raise ValueError("Checkpoint file paths must be unique.")
        self._validate_candidate_evidence(by_path)
        self._validate_review_evidence(by_path)
        self._validate_generated_mappings()
        self._validate_workspace_evidence(by_path)
        self._validate_phase_requirements()
        return self

    def _validate_candidate_evidence(self, by_path: dict[str, CheckpointFile]) -> None:
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

    def _validate_review_evidence(self, by_path: dict[str, CheckpointFile]) -> None:
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

    def _validate_generated_mappings(self) -> None:
        outputs = {item.output_path for item in self.progress.candidates()}
        outputs.update(item.output_path for item in self.progress.reviews())
        mappings = [item.output_path for item in self.generated_mappings]
        if len(mappings) != len(set(mappings)) or not set(mappings) <= outputs:
            raise ValueError(
                "Generated mappings must uniquely reference carried outputs."
            )

    def _validate_workspace_evidence(self, by_path: dict[str, CheckpointFile]) -> None:
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

    def _validate_phase_requirements(self) -> None:
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


class ClosedReviewCheckpoint(CheckpointIdentity):
    """Terminal marker carrying identity and a reason, with no resumable progress."""

    kind: Literal["closed"] = "closed"
    terminal_reason: str = Field(min_length=1)


type ReviewLoopCheckpoint = Annotated[
    OpenReviewCheckpoint | ClosedReviewCheckpoint, Field(discriminator="kind")
]

REVIEW_CHECKPOINT_ADAPTER: TypeAdapter[ReviewLoopCheckpoint] = TypeAdapter(
    ReviewLoopCheckpoint
)


def checkpoint_node_is_eligible(node: PreflightExecutionNode) -> bool:
    """Return whether a sequential node has executors followed by reviewers.

    Require both roles; perform no mutation or I/O.
    """
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
    """Validate node identity and, for open markers, progress, cursor, and paths.

    Closed markers return after identity and ordered provider topology checks.
    Open markers also check policy, invocation coordinates, stage membership,
    and output naming. This does not revalidate model references or read files.
    Neither input is mutated, and no I/O is performed.

    Raises:
        ValueError: The first violated checkpoint/node constraint.
    """
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
    review_checkpoint_progress.validate_checkpoint_progress(checkpoint, node)
    _validate_node_cursor(checkpoint, node)
    _validate_invocation_coordinates(checkpoint, node)
    _validate_artifact_paths(checkpoint, node)


def _validate_node_cursor(
    checkpoint: OpenReviewCheckpoint, node: PreflightExecutionNode
) -> None:
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


def _validate_invocation_coordinates(
    checkpoint: OpenReviewCheckpoint, node: PreflightExecutionNode
) -> None:
    identities = {item.task_id: item.role for item in checkpoint.tasks}
    max_round = (node.execution_policy.depth or 1) + 1
    records: list[
        CheckpointFile
        | CheckpointCandidate
        | CheckpointReview
        | CheckpointReviewerFailure
        | CheckpointWorkspace
    ] = [
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


def _validate_artifact_paths(
    checkpoint: OpenReviewCheckpoint, node: PreflightExecutionNode
) -> None:
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
    audit_rounds = node.execution_policy.audit_rounds or 1
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
