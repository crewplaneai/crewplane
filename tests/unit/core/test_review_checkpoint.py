from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from crewplane.core.execution_state import (
    ResumeOrigin,
    ReviewCheckpointResumeSummary,
    RunManifest,
)
from crewplane.core.review_checkpoint import (
    REVIEW_CHECKPOINT_ADAPTER,
    ClosedReviewCheckpoint,
    OpenReviewCheckpoint,
    validate_checkpoint_node,
)
from tests.helpers.resume import iso_datetime, make_plan, make_run_manifest
from tests.helpers.review_checkpoints import checkpoint_payload


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
