from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from crewplane.core.execution_state import (
    ResumeOrigin,
    ReviewCheckpointResumeSummary,
    RunManifest,
)
from crewplane.core.preflight.models import PreflightExecutionNode
from crewplane.core.review_checkpoint import (
    REVIEW_CHECKPOINT_ADAPTER,
    ClosedReviewCheckpoint,
    OpenReviewCheckpoint,
    validate_checkpoint_node,
)
from crewplane.core.review_checkpoint_state import (
    CheckpointEvaluation,
    CheckpointGeneratedMapping,
    CheckpointReview,
    CheckpointReviewerFailure,
    CheckpointTask,
    CheckpointWorkspace,
)
from crewplane.core.workflow.keywords import ProviderRole
from tests.helpers.resume import iso_datetime, make_plan, make_run_manifest
from tests.helpers.review_checkpoints import checkpoint_payload


@pytest.fixture
def checkpoint() -> OpenReviewCheckpoint:
    return OpenReviewCheckpoint.model_validate(checkpoint_payload())


@pytest.fixture
def settled_checkpoint(
    checkpoint: OpenReviewCheckpoint,
) -> tuple[OpenReviewCheckpoint, PreflightExecutionNode]:
    checkpoint = checkpoint.model_copy(deep=True)
    node = make_plan(review_loop=True).nodes[0]
    reviewer = node.provider_records[-1]
    node.provider_records.extend(
        reviewer.model_copy(update={"task_id": task_id})
        for task_id in ("review_middle", "review_last")
    )
    node.execution_policy.continue_on_failure = True
    checkpoint.tasks = [
        CheckpointTask(task_id=task.task_id, role=task.role)
        for task in node.provider_records
    ]
    evaluation = CheckpointEvaluation(
        verdict="NO_FINDINGS",
        approved=True,
        major_issues="None",
        minor_issues="None",
        nitpicks="None",
        unresolved_fingerprints=(),
        unresolved_issue_count=0,
        normalized_markdown="review",
        raw_text="review",
        evaluation_kind="structured",
        warnings=(),
    )
    reviews = [
        CheckpointReview(
            task_id=task_id,
            role=ProviderRole.REVIEWER,
            audit=1,
            local_round=1,
            output_path=f"a/{task_id}_round1.md",
            evaluation=evaluation,
        )
        for task_id in (reviewer.task_id, "review_last")
    ]
    checkpoint.files.extend(
        checkpoint.files[0].model_copy(
            update={
                "task_id": review.task_id,
                "role": review.role,
                "purpose": "reviewer_output",
                "relative_path": review.output_path,
            }
        )
        for review in reviews
    )
    progress = checkpoint.progress
    assert progress.active_audit is not None
    progress.latest_executor_outputs = progress.active_audit.executor_outputs
    progress.active_audit = None
    progress.latest_reviewer_outputs = reviews
    progress.reviewer_failures = [
        CheckpointReviewerFailure(
            task_id="review_middle",
            role=ProviderRole.REVIEWER,
            audit=1,
            local_round=1,
            failure_kind="invocation_failed",
            warning="review failed",
        )
    ]
    progress.selected_round_num = progress.last_round_num = 1
    progress.stop_reason = "no_progress"
    progress.continued_after_stop = True
    checkpoint.next_phase = "finalize"
    return checkpoint, node


def test_validation_returns_self_and_preserves_data(
    checkpoint: OpenReviewCheckpoint,
    settled_checkpoint: tuple[OpenReviewCheckpoint, PreflightExecutionNode],
) -> None:
    for record, node in (
        (checkpoint, make_plan(review_loop=True).nodes[0]),
        settled_checkpoint,
    ):
        before = record.model_dump()
        node_before = node.model_dump()
        assert record.validate_references() is record
        assert validate_checkpoint_node(record, node) is None
        assert record.model_dump() == before
        assert node.model_dump() == node_before


@pytest.mark.parametrize(
    "damage,message",
    [
        ("duplicate", "Checkpoint file paths must be unique."),
        ("candidate", "Candidate lacks matching producer evidence."),
        ("review", "Review lacks matching invocation evidence."),
        ("mapping", "Generated mappings must uniquely reference carried outputs."),
        ("destinations", "Workspace destinations must be unique."),
        ("workspace", "Workspace snapshot lacks matching invocation evidence."),
    ],
)
def test_reference_validation_preserves_first_error(
    settled_checkpoint: tuple[OpenReviewCheckpoint, PreflightExecutionNode],
    damage: str,
    message: str,
) -> None:
    record, _ = settled_checkpoint
    record.generated_mappings = [
        CheckpointGeneratedMapping(output_path="missing.md", snapshot_path=None)
    ]
    workspace = CheckpointWorkspace(
        task_id=record.tasks[0].task_id,
        role=ProviderRole.EXECUTOR,
        audit=1,
        local_round=1,
        snapshot_path="missing.json",
        destination_path="workspace.json",
    )
    record.workspaces = [workspace, workspace]
    if damage == "duplicate":
        record.files.append(record.files[0])
        record.files[0].purpose = "review_metadata"
    elif damage == "candidate":
        record.files[0].purpose = "review_metadata"
        record.files[1].purpose = "review_metadata"
    elif damage == "review":
        record.files[1].purpose = "review_metadata"
    elif damage in {"destinations", "workspace"}:
        record.generated_mappings = []
        if damage == "workspace":
            record.workspaces = [workspace]
    before = record.model_dump()
    with pytest.raises(ValueError) as caught:
        record.validate_references()
    assert str(caught.value) == message
    assert record.model_dump() == before


@pytest.mark.parametrize(
    "damage,message",
    [
        ("topology", "Checkpoint node identity or provider topology mismatch."),
        ("group", "Checkpoint candidates require the complete ordered executor phase."),
        ("conflict", "Checkpoint contains conflicting candidate identities."),
        ("producer", "Only fresh audits may seed an earlier producer candidate."),
        ("audit", "Checkpoint progress and audit cursor disagree."),
        ("bounds", "Checkpoint cursor exceeds configured review bounds."),
        ("invocation", "Checkpoint invocation identity or coordinates mismatch."),
        ("stage", "Checkpoint dependencies must belong to the compiled node stage."),
        ("path", "Checkpoint output path does not match its selected coordinates."),
    ],
)
def test_node_validation_preserves_first_error(
    checkpoint: OpenReviewCheckpoint, damage: str, message: str
) -> None:
    node = make_plan(review_loop=True).nodes[0]
    active = checkpoint.progress.active_audit
    assert active is not None
    candidate = active.executor_outputs[0]
    checkpoint.files[0].relative_path = "outside/candidate.md"
    if damage == "topology":
        checkpoint.node_id = "other"
        active.executor_outputs = []
    elif damage == "group":
        active.executor_outputs = []
        checkpoint.progress.executed_audit_rounds = 2
    elif damage == "conflict":
        active.latest_valid_executor_outputs = [candidate.model_copy(deep=True)]
        active.latest_valid_executor_outputs[0].identity.reason = "different"
        checkpoint.progress.executed_audit_rounds = 2
    elif damage == "producer":
        candidate.producer_round = 2
        checkpoint.progress.executed_audit_rounds = 2
    elif damage == "audit":
        checkpoint.progress.executed_audit_rounds = 2
        checkpoint.local_round = 3
    elif damage == "bounds":
        checkpoint.audit = checkpoint.progress.executed_audit_rounds = 2
        for carried in checkpoint.progress.candidates():
            carried.audit = 2
        checkpoint.files[0].task_id = "other"
    elif damage == "invocation":
        checkpoint.files[0].task_id = "other"
    elif damage == "path":
        candidate.output_path = "a/wrong.md"
        checkpoint.files[0].relative_path = candidate.output_path
    before = checkpoint.model_dump()
    with pytest.raises(ValueError) as caught:
        validate_checkpoint_node(checkpoint, node)
    assert str(caught.value) == message
    assert checkpoint.model_dump() == before


@pytest.mark.parametrize(
    "damage,message",
    [
        ("none", None),
        ("order", "Checkpoint reviewer order does not match the compiled node."),
        ("missing", "Checkpoint requires a complete settled reviewer batch."),
        ("coordinates", "Checkpoint requires a complete settled reviewer batch."),
        ("policy", "Reviewer failures require continuation policy."),
        ("consensus", "Reviewer failures prevent consensus."),
    ],
)
def test_settled_reviewer_batch_preserves_order_and_failure_policy(
    settled_checkpoint: tuple[OpenReviewCheckpoint, PreflightExecutionNode],
    damage: str,
    message: str | None,
) -> None:
    record, node = settled_checkpoint
    if damage == "order":
        record.progress.latest_reviewer_outputs.reverse()
    elif damage == "missing":
        record.progress.reviewer_failures = []
    elif damage == "coordinates":
        record.progress.reviewer_failures[0].local_round = 0
    elif damage == "policy":
        node.execution_policy.continue_on_failure = False
    elif damage == "consensus":
        record.progress.consensus_reached = True
    if message is None:
        assert validate_checkpoint_node(record, node) is None
    else:
        with pytest.raises(ValueError) as caught:
            validate_checkpoint_node(record, node)
        assert str(caught.value) == message


def test_closed_checkpoint_only_requires_node_identity(
    checkpoint: OpenReviewCheckpoint,
) -> None:
    identity = {
        key: value
        for key, value in checkpoint.model_dump().items()
        if key in ClosedReviewCheckpoint.model_fields and key != "kind"
    }
    closed = ClosedReviewCheckpoint(**identity, terminal_reason="failed")
    node = make_plan(review_loop=True).nodes[0]
    node.artifact_contract.stage_path = None
    before = closed.model_dump()
    assert validate_checkpoint_node(closed, node) is None
    assert closed.model_dump() == before
    closed.node_id = "other"
    with pytest.raises(ValueError, match="topology mismatch"):
        validate_checkpoint_node(closed, node)


def test_strict_open_and_closed_shapes() -> None:
    payload = checkpoint_payload()
    record = REVIEW_CHECKPOINT_ADAPTER.validate_python(payload)
    assert REVIEW_CHECKPOINT_ADAPTER.validate_json(record.model_dump_json()) == record
    identity = {
        key: value
        for key, value in payload.items()
        if key in ClosedReviewCheckpoint.model_fields and key != "kind"
    }
    closed = ClosedReviewCheckpoint(**identity, terminal_reason="no_progress")
    validate_checkpoint_node(closed, make_plan(review_loop=True).nodes[0])
    assert closed.kind == "closed"
    for changes in (
        {"extra": True},
        {"kind": "closed", "terminal_reason": "failed"},
        {"next_phase": "apply_review"},
        {"run_state_schema_version": 99},
        {"plan_schema_version": "0.0"},
    ):
        with pytest.raises(ValidationError):
            REVIEW_CHECKPOINT_ADAPTER.validate_python({**payload, **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"audit": 0},
        {"local_round": -1},
        {"audit": True},
        {"audit": 2},
        {"local_round": 3},
        {"local_round": 0},
        {"local_round": 2, "next_phase": "executors"},
        {"node_id": "b"},
        {"next_phase": "finalize"},
        {"files": []},
        {"tasks": []},
    ],
)
def test_rejects_illegal_cursors_topology_and_references(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        checkpoint = OpenReviewCheckpoint.model_validate(
            {**checkpoint_payload(), **changes}
        )
        validate_checkpoint_node(checkpoint, make_plan(review_loop=True).nodes[0])


def test_remediation_without_active_progress_is_rejected_by_the_model() -> None:
    payload = checkpoint_payload()
    payload.update(
        next_phase="executors",
        local_round=2,
        progress={"executed_audit_rounds": 1},
        files=[],
    )
    with pytest.raises(ValidationError, match="require active audit progress"):
        OpenReviewCheckpoint.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("relative_path", "../candidate.md"),
        ("purpose", "reviewer_output"),
        ("task_id", "other"),
        ("local_round", 2),
        ("signature", (9, "bad")),
        ("signature", (-1, "a" * 64)),
        ("unknown", True),
    ],
)
def test_rejects_invalid_descriptor_relationships(field: str, value: object) -> None:
    payload = deepcopy(checkpoint_payload())
    payload["files"][0][field] = value
    with pytest.raises(ValueError):
        checkpoint = OpenReviewCheckpoint.model_validate(payload)
        validate_checkpoint_node(checkpoint, make_plan(review_loop=True).nodes[0])


def test_ineligible_node_and_ordered_roles_are_rejected() -> None:
    record = OpenReviewCheckpoint.model_validate(checkpoint_payload())
    with pytest.raises(ValueError, match="topology"):
        validate_checkpoint_node(record, make_plan().nodes[0])
    payload = checkpoint_payload()
    payload["tasks"].reverse()
    with pytest.raises(ValueError, match="topology"):
        validate_checkpoint_node(
            OpenReviewCheckpoint.model_validate(payload),
            make_plan(review_loop=True).nodes[0],
        )


@pytest.mark.parametrize(
    "nodes,checkpoints", [(["b"], []), ([], ["a"]), (["b"], ["a"])]
)
def test_manifest_source_covers_successful_nodes_and_checkpoints(
    nodes: list[str], checkpoints: list[str]
) -> None:
    manifest = make_run_manifest("fresh", "workflow--fresh").model_dump()
    summaries = [
        ReviewCheckpointResumeSummary(
            node_id=node,
            source_audit=1,
            source_local_round=1,
            source_phase="reviewers",
            resume_origin=ResumeOrigin(
                source_run_id="source",
                source_run_key_name="workflow--source",
                source_node_id=node,
                hydrated_at=iso_datetime(),
            ),
        )
        for node in checkpoints
    ]
    manifest.update(
        resumed_nodes=nodes,
        resumed_review_checkpoints=summaries,
        resume_source_run_id="source",
        resume_source_run_key_name="workflow--source",
    )
    result = RunManifest.model_validate(manifest)
    assert result.resumed_nodes == nodes
    if summaries:
        with pytest.raises(ValueError, match="distinct"):
            RunManifest.model_validate({**manifest, "resumed_nodes": ["a"]})
        with pytest.raises(ValueError, match="source run"):
            RunManifest.model_validate(
                {**manifest, "resume_source_run_id": "different"}
            )
