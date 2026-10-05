from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.artifacts.resume.checkpoint_files import read_checkpoint_file
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.core.review_checkpoint_state import (
    CheckpointEvaluation,
    CheckpointFile,
    CheckpointReview,
)
from crewplane.runtime.execution.review_loop import checkpoint_progress
from crewplane.runtime.execution.review_loop.checkpoint_progress import (
    ProgressRestorer,
    encode_progress,
)
from crewplane.runtime.execution.review_loop.state import (
    render_unresolved_review_packet,
)
from crewplane.runtime.execution.review_loop.types import ReviewerRoundArtifact
from tests.helpers.resume import make_plan
from tests.helpers.review_checkpoints import checkpoint_payload


@pytest.fixture
def checkpoint(tmp_path: Path) -> OpenReviewCheckpoint:
    stored = OpenReviewCheckpoint.model_validate(checkpoint_payload())
    active = stored.progress.active_audit
    assert active is not None
    candidate = active.executor_outputs[0]
    descriptor = stored.files[0]
    candidates = []
    stored.files = []
    for name in ("loop", "current", "previous", "selected"):
        path = tmp_path / "a" / f"{name}.md"
        path.parent.mkdir(exist_ok=True)
        payload = f"{name} candidate".encode()
        path.write_bytes(payload)
        relative = path.relative_to(tmp_path).as_posix()
        candidates.append(candidate.model_copy(update={"output_path": relative}))
        stored.files.append(
            descriptor.model_copy(
                update={
                    "relative_path": relative,
                    "signature": (len(payload), hashlib.sha256(payload).hexdigest()),
                }
            )
        )
    stored.progress.latest_executor_outputs = candidates[:1]
    active.executor_outputs = candidates[1:2]
    active.previous_executor_outputs = candidates[2:3]
    active.latest_valid_executor_outputs = candidates[3:]
    reviewer = make_plan(review_loop=True).nodes[0].provider_records[-1]
    review = CheckpointReview(
        task_id=reviewer.task_id,
        role=reviewer.role,
        audit=1,
        local_round=1,
        output_path="a/review.md",
        evaluation=CheckpointEvaluation(
            verdict=None,
            approved=False,
            major_issues="None",
            minor_issues="None",
            nitpicks="None",
            unresolved_fingerprints=(),
            unresolved_issue_count=0,
            normalized_markdown="normalized feedback",
            raw_text="raw feedback",
            evaluation_kind="unstructured_feedback",
            warnings=("unstructured review",),
            unstructured_feedback="Please correct the candidate.",
        ),
    )
    stored.files.append(
        descriptor.model_copy(
            update={
                "task_id": review.task_id,
                "role": review.role,
                "purpose": "reviewer_output",
                "relative_path": review.output_path,
            }
        )
    )
    stored.progress.latest_reviewer_outputs = [review]
    active.latest_reviewer_outputs = [review]
    return OpenReviewCheckpoint.model_validate(stored.model_dump())


@pytest.fixture
def restorer(checkpoint: OpenReviewCheckpoint, tmp_path: Path) -> ProgressRestorer:
    return ProgressRestorer(
        checkpoint, tmp_path, make_plan(review_loop=True).nodes[0].provider_records, 3
    )


@pytest.mark.parametrize("previous", ["absent", "empty", "populated"])
def test_restore_preserves_feedback_presence_and_state_ownership(
    restorer: ProgressRestorer, previous: str
) -> None:
    stored = restorer.checkpoint.progress
    active = stored.active_audit
    assert active is not None
    if previous == "absent":
        active.previous_executor_outputs = None
    elif previous == "empty":
        active.previous_executor_outputs = []
    before = restorer.checkpoint.model_dump()

    progress = restorer.restore()

    restored = progress.active_audit
    assert restored is not None
    assert restored.stall is progress.stall
    assert restored.stall is not stored.stall
    expected_feedback = (
        None
        if previous == "absent"
        else render_unresolved_review_packet(restored.latest_reviewer_outputs)
    )
    assert restored.previous_review_packet == expected_feedback
    assert (restored.previous_executor_outputs is None) == (previous == "absent")
    assert encode_progress(restorer.root, progress) == stored
    assert restorer.checkpoint.model_dump() == before
    assert progress.latest_reviewer_outputs is not stored.latest_reviewer_outputs
    assert progress.reviewer_failures is not stored.reviewer_failures
    assert progress.initial_reviews is not stored.initial_reviews
    assert progress.initial_failures is not stored.initial_failures
    assert restored.reviewer_failures is not active.reviewer_failures
    restored.stall.consecutive_round_count += 1
    progress.latest_reviewer_outputs.clear()
    assert restorer.checkpoint.model_dump() == before


def test_restore_reads_and_reconstructs_feedback_in_order(
    restorer: ProgressRestorer, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    def read(root: Path, descriptor: CheckpointFile) -> bytes:
        events.append(descriptor.relative_path)
        return read_checkpoint_file(root, descriptor)

    def render(reviews: list[ReviewerRoundArtifact]) -> str | None:
        events.append("feedback")
        return render_unresolved_review_packet(reviews)

    monkeypatch.setattr(checkpoint_progress, "read_checkpoint_file", read)
    monkeypatch.setattr(checkpoint_progress, "render_unresolved_review_packet", render)

    restorer.restore()

    assert events == [
        "a/loop.md",
        "a/current.md",
        "a/previous.md",
        "feedback",
        "a/selected.md",
    ]
    assert not (restorer.root / "a/review.md").exists()


@pytest.mark.parametrize("damage", ["missing", "changed", "non_utf8"])
def test_restore_propagates_first_candidate_read_failure_without_mutation(
    restorer: ProgressRestorer, damage: str
) -> None:
    path = restorer.root / "a/current.md"
    before = restorer.checkpoint.model_dump()
    if damage == "missing":
        path.unlink()
        error, message = ValueError, "Checkpoint dependency is missing or unsafe"
    elif damage == "changed":
        path.write_bytes(b"changed")
        error, message = ValueError, "Checkpoint dependency changed"
    else:
        payload = b"\xff"
        path.write_bytes(payload)
        restorer.files["a/current.md"].signature = (
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )
        before = restorer.checkpoint.model_dump()
        error, message = UnicodeDecodeError, "utf-8"
    (restorer.root / "a/selected.md").unlink()

    with pytest.raises(error, match=message):
        restorer.restore()

    assert restorer.checkpoint.model_dump() == before
    if damage != "missing":
        assert path.read_bytes() == (b"changed" if damage == "changed" else b"\xff")


def test_encode_reports_active_audit_error_before_loop_error(
    restorer: ProgressRestorer,
) -> None:
    progress = restorer.restore()
    assert progress.active_audit is not None
    assert progress.latest_executor_outputs is not None
    active = progress.active_audit.executor_outputs[0]
    progress.active_audit.executor_outputs = [replace(active, candidate_identity=None)]
    latest = progress.latest_executor_outputs[0]
    progress.latest_executor_outputs = [
        replace(latest, output_file=restorer.root.parent / "outside.md")
    ]

    with pytest.raises(ValueError, match="Checkpoint candidate lacks a bound identity"):
        encode_progress(restorer.root, progress)
