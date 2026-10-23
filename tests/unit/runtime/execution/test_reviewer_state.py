from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from crewplane.artifacts.resume.checkpoint_files import describe_review_evidence
from crewplane.core.review_checkpoint_state import (
    CheckpointEvaluation,
    CheckpointProgress,
    CheckpointReview,
    CheckpointReviewerFailure,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.consensus import EvaluatedReviewResult
from crewplane.runtime.execution.review_loop.state import (
    persist_review_evaluation_artifacts,
    persist_review_state,
    persist_reviewer_failure_state,
)
from crewplane.runtime.execution.review_loop.types import (
    ReviewerInvocationFailure,
    ReviewerRoundArtifact,
)
from tests.helpers.resume import make_provider_record


def reviewer_artifact(artifact_dir: Path) -> ReviewerRoundArtifact:
    return ReviewerRoundArtifact(
        provider=make_provider_record("reviewer", ProviderRole.REVIEWER),
        task_id=" Review/Task_0 ",
        evaluation=EvaluatedReviewResult(
            verdict="CHANGES_REQUESTED",
            approved=False,
            major_issues="NONE",
            minor_issues="- Fix é",
            nitpicks="NONE",
            unresolved_fingerprints=("fingerprint",),
            unresolved_issue_count=1,
            normalized_markdown="review",
            raw_text="raw review",
            evaluation_kind="structured",
            warnings=("warning é",),
            original_verdict="CHANGES_REQUESTED",
        ),
        output_file=artifact_dir / "review.md",
        audit_round_num=None,
        round_num=1,
    )


@pytest.mark.parametrize("audit_rounds", [1, 3])
@pytest.mark.parametrize("round_num", [0, 2])
@pytest.mark.parametrize("failed", [False, True])
def test_checkpoint_discovers_published_reviewer_state(
    tmp_path: Path, audit_rounds: int, round_num: int, failed: bool
) -> None:
    audit = audit_rounds
    stage_dir = tmp_path / "custom" / "stage"
    artifact_dir = (
        stage_dir if audit_rounds == 1 else stage_dir / f"review-audit-round-{audit}"
    )
    artifact = reviewer_artifact(artifact_dir)
    if failed:
        failure = ReviewerInvocationFailure(
            0,
            artifact.provider,
            artifact.task_id,
            artifact.output_file,
            RuntimeError("interrupted"),
            "invocation_failed",
            "interrupted",
        )
        state_path = persist_reviewer_failure_state(
            artifact_dir, audit, round_num, failure
        )
        progress = CheckpointProgress(
            reviewer_failures=[
                CheckpointReviewerFailure(
                    task_id=artifact.task_id,
                    role="reviewer",
                    audit=audit,
                    local_round=round_num,
                    failure_kind="invocation_failed",
                    warning="interrupted",
                )
            ]
        )
    else:
        state_path = persist_review_state(artifact_dir, audit, round_num, artifact)
        persist_review_evaluation_artifacts(artifact.output_file, artifact.evaluation)
        progress = CheckpointProgress(
            latest_reviewer_outputs=[
                CheckpointReview(
                    task_id=artifact.task_id,
                    role="reviewer",
                    audit=audit,
                    local_round=round_num,
                    output_path=artifact.output_file.relative_to(tmp_path).as_posix(),
                    evaluation=CheckpointEvaluation.model_validate(
                        asdict(artifact.evaluation)
                    ),
                )
            ]
        )
    before = state_path.read_bytes()

    files = describe_review_evidence(tmp_path, progress, "custom/stage", audit_rounds)

    state = next(
        file for file in files if file.purpose in {"review_state", "review_failure"}
    )
    assert state.relative_path == state_path.relative_to(tmp_path).as_posix()
    assert state.local_round == round_num
    assert state.audit == audit
    assert state.signature[0] == len(before)
    assert state_path.read_bytes() == before


@pytest.mark.parametrize(("audit_round", "round_num"), [(None, 0), (None, 1), (2, 1)])
@pytest.mark.parametrize("state_kind", ["success", "failure_missing", "failure_output"])
@pytest.mark.parametrize(
    ("output_name", "raw_name", "metadata_name"),
    [
        ("review.md", "review.raw.txt", "review.review.json"),
        ("review.v2.md", "review.v2.raw.txt", "review.v2.review.json"),
    ],
)
def test_reviewer_state_filename_and_bytes(
    tmp_path, audit_round, round_num, state_kind, output_name, raw_name, metadata_name
) -> None:
    artifact_dir = tmp_path if audit_round is None else tmp_path / "audit-2"
    artifact = reviewer_artifact(artifact_dir)
    artifact = replace(artifact, output_file=artifact_dir / output_name)
    expected = {
        "reviewer": "reviewer",
        "task_id": " Review/Task_0 ",
        "audit_round_num": audit_round,
        "round_num": round_num,
        "approved": False,
    }
    if state_kind == "success":
        path = persist_review_state(artifact_dir, audit_round, round_num, artifact)
        expected.update(
            verdict="CHANGES_REQUESTED",
            evaluation_kind="structured",
            original_verdict="CHANGES_REQUESTED",
            had_leading_text=False,
            had_trailing_text=False,
            major_issues="NONE",
            minor_issues="- Fix é",
            nitpicks="NONE",
            unstructured_feedback=None,
            unresolved_fingerprints=["fingerprint"],
            unresolved_issue_count=1,
            warnings=["warning é"],
            normalized_output_artifact=output_name,
            raw_output_artifact=raw_name,
            metadata_artifact=metadata_name,
        )
    else:
        if state_kind == "failure_output":
            artifact_dir.mkdir(parents=True, exist_ok=True)
            artifact.output_file.write_text("partial output", encoding="utf-8")
        failure = ReviewerInvocationFailure(
            0,
            artifact.provider,
            artifact.task_id,
            artifact.output_file,
            RuntimeError("failed é"),
            "invocation",
            "warning é",
        )
        path = persist_reviewer_failure_state(
            artifact_dir, audit_round, round_num, failure
        )
        expected.update(
            evaluation_kind="reviewer_failure",
            failure_kind="invocation",
            failure_message="failed é",
            output_artifact=output_name if state_kind == "failure_output" else None,
            warnings=["warning é"],
        )
    assert (
        path
        == artifact_dir / "review-state" / f"review-task-0-round-{round_num}.state.json"
    )
    assert path.read_bytes() == json.dumps(
        expected, indent=2, sort_keys=True, allow_nan=False
    ).encode("utf-8")
    assert not path.read_bytes().endswith(b"\n")


@pytest.mark.parametrize("state_kind", ["success", "failure"])
@pytest.mark.parametrize("invalid_value", [float("nan"), object()])
def test_reviewer_state_serialization_failure_publishes_nothing(
    tmp_path, state_kind, invalid_value
) -> None:
    artifact_dir = tmp_path / "uncreated"
    artifact = reviewer_artifact(artifact_dir)
    with pytest.raises((TypeError, ValueError)):
        if state_kind == "success":
            artifact = replace(
                artifact,
                evaluation=replace(artifact.evaluation, warnings=(invalid_value,)),
            )
            persist_review_state(artifact_dir, None, 1, artifact)
        else:
            failure = ReviewerInvocationFailure(
                0,
                artifact.provider,
                artifact.task_id,
                artifact.output_file,
                RuntimeError("failed"),
                "invocation",
                invalid_value,
            )
            persist_reviewer_failure_state(artifact_dir, None, 1, failure)
    assert not artifact_dir.exists()


@pytest.mark.parametrize(
    ("kind", "verdict", "original_verdict", "feedback"),
    [
        ("structured", "CHANGES_REQUESTED", "CHANGES_REQUESTED", None),
        ("structured", "CHANGES_REQUESTED", "APPROVED", None),
        ("unstructured", None, None, "raw feedback é"),
    ],
    ids=["structured", "normalized", "unstructured"],
)
def test_review_artifacts_preserve_distinct_projections(
    tmp_path, kind, verdict, original_verdict, feedback
) -> None:
    artifact = reviewer_artifact(tmp_path)
    artifact = replace(
        artifact,
        evaluation=replace(
            artifact.evaluation,
            evaluation_kind=kind,
            verdict=verdict,
            original_verdict=original_verdict,
            unstructured_feedback=feedback,
            had_leading_text=True,
            had_trailing_text=True,
        ),
    )
    persist_review_evaluation_artifacts(artifact.output_file, artifact.evaluation)
    state_path = persist_review_state(tmp_path, None, 0, artifact)
    shared = {
        "approved": False,
        "evaluation_kind": kind,
        "original_verdict": original_verdict,
        "had_leading_text": True,
        "had_trailing_text": True,
        "unstructured_feedback": feedback,
        "unresolved_issue_count": 1,
        "warnings": ["warning é"],
    }
    metadata = {**shared, "normalized_verdict": verdict}
    state = {
        **shared,
        "verdict": verdict,
        "major_issues": "NONE",
        "minor_issues": "- Fix é",
        "nitpicks": "NONE",
        "unresolved_fingerprints": ["fingerprint"],
        "reviewer": "reviewer",
        "task_id": " Review/Task_0 ",
        "audit_round_num": None,
        "round_num": 0,
        "normalized_output_artifact": "review.md",
        "raw_output_artifact": "review.raw.txt",
        "metadata_artifact": "review.review.json",
    }
    metadata_path = tmp_path / "review.review.json"
    assert metadata_path.read_bytes() == (
        json.dumps(metadata, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    assert state_path.read_bytes() == json.dumps(
        state, indent=2, sort_keys=True, allow_nan=False
    ).encode("utf-8")
    assert (tmp_path / "review.raw.txt").read_text() == "raw review"
