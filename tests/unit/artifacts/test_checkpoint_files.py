from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from crewplane.artifacts.resume import checkpoint_files
from crewplane.core.review_checkpoint_state import (
    CheckpointAuditProgress,
    CheckpointCandidate,
    CheckpointCandidateIdentity,
    CheckpointEvaluation,
    CheckpointFile,
    CheckpointProgress,
    CheckpointReview,
    CheckpointReviewerFailure,
)


@pytest.fixture
def dependency(tmp_path: Path) -> CheckpointFile:
    payload = b"candidate"
    (tmp_path / "candidate.md").write_bytes(payload)
    return CheckpointFile(
        task_id="executor",
        role="executor",
        audit=1,
        local_round=1,
        relative_path="candidate.md",
        purpose="executor_output",
        signature=(len(payload), hashlib.sha256(payload).hexdigest()),
    )


@pytest.mark.parametrize("signature_supplied", [False, True])
def test_describe_checkpoint_file_preserves_metadata_and_signature(
    tmp_path: Path, dependency: CheckpointFile, signature_supplied: bool
) -> None:
    assert (
        checkpoint_files.describe_checkpoint_file(
            tmp_path,
            dependency.relative_path,
            dependency,
            dependency.purpose,
            dependency.signature if signature_supplied else None,
        )
        == dependency
    )


def test_verify_checkpoint_files_returns_none_without_mutating_descriptors(
    tmp_path: Path, dependency: CheckpointFile
) -> None:
    before = dependency.model_dump()
    assert checkpoint_files.verify_checkpoint_files(tmp_path, []) is None
    assert checkpoint_files.verify_checkpoint_files(tmp_path, [dependency]) is None
    assert dependency.model_dump() == before


def test_verification_captures_path_and_purpose_before_hashing(
    tmp_path: Path, dependency: CheckpointFile, monkeypatch: pytest.MonkeyPatch
) -> None:
    hash_file = checkpoint_files.file_size_and_sha256

    def mutate_descriptor_during_hash(path: Path) -> tuple[int, str]:
        dependency.relative_path = "../changed.md"
        dependency.purpose = "unknown"
        return hash_file(path)

    monkeypatch.setattr(
        checkpoint_files, "file_size_and_sha256", mutate_descriptor_during_hash
    )
    assert checkpoint_files.verify_checkpoint_files(tmp_path, [dependency]) is None


@pytest.mark.parametrize("operation", ["describe", "verify"])
@pytest.mark.parametrize("damage", ["missing", "unsafe", "changed", "metadata"])
def test_checkpoint_dependency_error_precedence(
    tmp_path: Path, dependency: CheckpointFile, operation: str, damage: str
) -> None:
    descriptor = dependency.model_copy(
        update={
            "task_id": "",
            "role": "unknown",
            "audit": 0,
            "local_round": -1,
            "purpose": "unknown",
        }
    )
    path = tmp_path / descriptor.relative_path
    if damage in {"missing", "unsafe"}:
        descriptor = descriptor.model_copy(update={"signature": (0, "0" * 64)})
        path.unlink()
        if damage == "unsafe":
            path.symlink_to(tmp_path / "target.md")
            (tmp_path / "target.md").write_bytes(b"candidate")
    elif damage == "changed":
        path.write_bytes(b"different")

    with pytest.raises(ValueError) as caught:
        if operation == "describe":
            checkpoint_files.describe_checkpoint_file(
                tmp_path,
                descriptor.relative_path,
                descriptor,
                descriptor.purpose,
                descriptor.signature,
            )
        else:
            checkpoint_files.verify_checkpoint_files(tmp_path, [descriptor])

    if damage != "metadata":
        assert type(caught.value) is ValueError
        reason = "changed" if damage == "changed" else "is missing or unsafe"
        assert str(caught.value) == f"Checkpoint dependency {reason}: candidate.md"
        return

    assert type(caught.value) is ValidationError
    assert str(caught.value).startswith("5 validation errors for CheckpointFile\n")
    assert [
        (error["loc"], error["type"], error["msg"])
        for error in caught.value.errors(include_url=False)
    ] == [
        (("task_id",), "string_too_short", "String should have at least 1 character"),
        (("role",), "enum", "Input should be 'executor' or 'reviewer'"),
        (
            ("audit",),
            "greater_than_equal",
            "Input should be greater than or equal to 1",
        ),
        (
            ("local_round",),
            "greater_than_equal",
            "Input should be greater than or equal to 0",
        ),
        (
            ("purpose",),
            "literal_error",
            "Input should be 'executor_output', 'reviewer_output', 'review_state', 'review_metadata', 'review_failure', 'candidate_identity', 'generated_file', 'generated_metadata', 'workspace_state', 'workspace_setup', 'workspace_rendered' or 'workspace_bundle'",
        ),
    ]


@pytest.mark.parametrize("rejection", ["missing", "changed", "metadata"])
def test_verification_stops_at_first_rejected_dependency(
    tmp_path: Path,
    dependency: CheckpointFile,
    monkeypatch: pytest.MonkeyPatch,
    rejection: str,
) -> None:
    rejected = dependency.model_copy(update={"relative_path": "rejected.md"})
    if rejection != "missing":
        payload = b"different" if rejection == "changed" else b"candidate"
        (tmp_path / rejected.relative_path).write_bytes(payload)
    if rejection == "metadata":
        rejected = rejected.model_copy(update={"task_id": ""})
    untouched = dependency.model_copy(update={"relative_path": "untouched.md"})
    inspected: list[str] = []
    resolve = checkpoint_files.contained_regular_file

    def trace_resolution(root: Path, relative_path: str) -> Path | None:
        inspected.append(relative_path)
        return resolve(root, relative_path)

    monkeypatch.setattr(checkpoint_files, "contained_regular_file", trace_resolution)
    exception = ValidationError if rejection == "metadata" else ValueError
    message = "task_id" if rejection == "metadata" else "rejected.md"
    with pytest.raises(exception, match=message):
        checkpoint_files.verify_checkpoint_files(
            tmp_path, [dependency, rejected, untouched]
        )
    assert inspected == ["candidate.md", "rejected.md"]


@pytest.fixture
def evidence_progress() -> CheckpointProgress:
    review = CheckpointReview(
        task_id="Review / Agent",
        role="reviewer",
        audit=1,
        local_round=0,
        output_path="stage/reviewer.md",
        evaluation=CheckpointEvaluation(
            verdict="NO_FINDINGS",
            approved=True,
            major_issues="",
            minor_issues="",
            nitpicks="",
            unresolved_fingerprints=(),
            unresolved_issue_count=0,
            normalized_markdown="approved",
            raw_text="approved",
            evaluation_kind="structured",
            warnings=(),
        ),
    )
    candidate = CheckpointCandidate(
        task_id="executor",
        role="executor",
        audit=3,
        local_round=3,
        output_path="stage/executor.md",
        producer_audit=1,
        producer_round=1,
        identity=CheckpointCandidateIdentity(kind="unverified", fingerprint=None),
    )
    return CheckpointProgress(
        initial_reviews=[review],
        latest_reviewer_outputs=[review.model_copy(update={"audit": 2})],
        reviewer_failures=[
            CheckpointReviewerFailure(
                task_id=review.task_id,
                role="reviewer",
                audit=3,
                local_round=0,
                failure_kind="invocation_failed",
                warning="review interrupted",
            )
        ],
        latest_executor_outputs=[candidate],
        active_audit=CheckpointAuditProgress(
            executor_outputs=[
                candidate.model_copy(update={"producer_audit": 2, "producer_round": 2})
            ]
        ),
    )


def evidence_paths(audit_rounds: int) -> list[str]:
    directory = "stage" if audit_rounds == 1 else "stage/review-audit-round-3"
    return [
        "stage/reviewer.review.json",
        "stage/reviewer.raw.txt",
        "stage/review-state/review-agent-round-0.state.json",
        f"{directory}/review-state/review-agent-round-0.state.json",
        "stage/executor.candidate.json",
    ]


@pytest.mark.parametrize("audit_rounds", [1, 3])
def test_review_evidence_preserves_order_and_replaces_duplicate_descriptors(
    tmp_path: Path, evidence_progress: CheckpointProgress, audit_rounds: int
) -> None:
    paths = evidence_paths(audit_rounds)
    payload = b"evidence"
    for relative in dict.fromkeys(paths):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    before = evidence_progress.model_dump()
    with patch.object(
        checkpoint_files,
        "contained_regular_file",
        wraps=checkpoint_files.contained_regular_file,
    ) as resolved:
        files = checkpoint_files.describe_review_evidence(
            tmp_path, evidence_progress, "stage", audit_rounds
        )
    assert [call.args[1] for call in resolved.call_args_list] == [
        *paths[:3],
        *paths[:3],
        paths[3],
        paths[4],
        paths[4],
    ]
    expected = [
        (paths[0], "review_metadata", "Review / Agent", "reviewer", 2, 0),
        (paths[1], "review_metadata", "Review / Agent", "reviewer", 2, 0),
    ]
    if audit_rounds > 1:
        expected.append((paths[2], "review_state", "Review / Agent", "reviewer", 2, 0))
    expected.extend(
        [
            (paths[3], "review_failure", "Review / Agent", "reviewer", 3, 0),
            (paths[4], "candidate_identity", "executor", "executor", 2, 2),
        ]
    )
    assert [
        (
            item.relative_path,
            item.purpose,
            item.task_id,
            item.role,
            item.audit,
            item.local_round,
        )
        for item in files
    ] == expected
    assert all(
        item.signature == (len(payload), hashlib.sha256(payload).hexdigest())
        for item in files
    )
    assert evidence_progress.model_dump() == before
    assert all((tmp_path / relative).read_bytes() == payload for relative in paths)
    assert (
        checkpoint_files.describe_review_evidence(
            tmp_path, CheckpointProgress(), "stage", audit_rounds
        )
        == []
    )


@pytest.mark.parametrize("missing_index", range(5))
def test_review_evidence_propagates_first_error_without_inspecting_later_files(
    tmp_path: Path, evidence_progress: CheckpointProgress, missing_index: int
) -> None:
    paths = evidence_paths(3)
    for relative in paths[:missing_index]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"evidence")
    with (
        patch.object(
            checkpoint_files,
            "contained_regular_file",
            wraps=checkpoint_files.contained_regular_file,
        ) as resolved,
        pytest.raises(ValueError) as caught,
    ):
        checkpoint_files.describe_review_evidence(
            tmp_path, evidence_progress, "stage", 3
        )
    assert type(caught.value) is ValueError
    assert str(caught.value) == (
        f"Checkpoint dependency is missing or unsafe: {paths[missing_index]}"
    )
    expected = paths[: missing_index + 1]
    if missing_index >= 3:
        expected = [*paths[:3], *expected]
    assert [call.args[1] for call in resolved.call_args_list] == expected
