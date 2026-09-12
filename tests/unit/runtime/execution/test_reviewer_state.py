from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.consensus import EvaluatedReviewResult
from crewplane.runtime.execution.review_loop.state import (
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


@pytest.mark.parametrize(("audit_round", "round_num"), [(None, 0), (None, 1), (2, 1)])
@pytest.mark.parametrize("state_kind", ["success", "failure_missing", "failure_output"])
def test_reviewer_state_filename_and_bytes(
    tmp_path, audit_round, round_num, state_kind
) -> None:
    artifact_dir = tmp_path if audit_round is None else tmp_path / "audit-2"
    artifact = reviewer_artifact(artifact_dir)
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
            normalized_output_artifact="review.md",
            raw_output_artifact="review.raw.txt",
            metadata_artifact="review.review.json",
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
            output_artifact="review.md" if state_kind == "failure_output" else None,
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
