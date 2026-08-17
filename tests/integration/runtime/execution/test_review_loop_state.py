import hashlib
import json
from pathlib import Path

import pytest

from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import ProviderSpec
from crewplane.runtime.execution.consensus import (
    ParsedReviewResult,
    evaluate_review_output,
    render_review_contract,
)
from crewplane.runtime.execution.review_loop.state import (
    build_review_loop_status_payload,
    persist_review_loop_status,
)
from crewplane.runtime.execution.review_loop.types import (
    AuditRoundResult,
    ExecutorRoundArtifact,
    ReviewerRoundArtifact,
    ReviewLoopProgress,
)


def _reviewer_artifact(node_dir: Path) -> ReviewerRoundArtifact:
    output_file = node_dir / "review_reviewer_0_round1.md"
    output_file.write_text("review", encoding="utf-8")
    evaluation = evaluate_review_output(
        render_review_contract(
            ParsedReviewResult(
                verdict="NO_FINDINGS",
                major_issues="None",
                minor_issues="None",
                nitpicks="None",
            )
        )
    )
    return ReviewerRoundArtifact(
        provider=ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
        task_id="review_reviewer_0",
        evaluation=evaluation,
        output_file=output_file,
        audit_round_num=2,
        round_num=3,
        output_signature=_file_signature(output_file),
    )


def _file_signature(path: Path) -> tuple[int, str]:
    payload = path.read_bytes()
    return len(payload), hashlib.sha256(payload).hexdigest()


def test_status_payload_shape_and_paths_are_relative_to_node_dir(
    tmp_path: Path,
) -> None:
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    executor_output = node_dir / "exec_executor_0_round1.md"
    executor_output.write_text("Candidate", encoding="utf-8")
    executor = ExecutorRoundArtifact(
        provider=ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
        task_id="exec_executor_0",
        content="Candidate",
        output_file=executor_output,
        audit_round_num=2,
        round_num=3,
        output_signature=_file_signature(executor_output),
    )
    reviewer = _reviewer_artifact(node_dir)
    progress = ReviewLoopProgress(
        latest_executor_outputs=[executor],
        latest_reviewer_outputs=[reviewer],
        executed_audit_rounds=2,
        last_round_num=3,
        consensus_reached=True,
        invalid_candidate_round_count=1,
        no_progress_round_count=1,
        artifact_drift_warning_count=2,
    )

    payload = build_review_loop_status_payload("review.node", node_dir, progress)

    assert list(payload) == [
        "node_id",
        "executed_audit_rounds",
        "attempted_local_round_num",
        "final_local_round_num",
        "consensus_reached",
        "continued_after_consensus_exhaustion",
        "invalid_candidate_round_count",
        "no_progress_round_count",
        "artifact_drift_warning_count",
        "canonical_executor_outputs",
        "reviewer_outputs",
    ]
    assert payload["canonical_executor_outputs"] == [
        {
            "task_id": "exec_executor_0",
            "provider": "exec",
            "role": "executor",
            "path": "exec_executor_0_round1.md",
            "sha256": hashlib.sha256(b"Candidate").hexdigest(),
            "size_bytes": 9,
            "audit_round_num": 2,
            "round_num": 3,
        }
    ]
    assert payload["reviewer_outputs"] == [
        {
            "task_id": "review_reviewer_0",
            "provider": "review",
            "role": "reviewer",
            "path": "review_reviewer_0_round1.md",
            "sha256": hashlib.sha256(b"review").hexdigest(),
            "size_bytes": 6,
            "audit_round_num": 2,
            "round_num": 3,
        }
    ]


def test_status_payload_rejects_symlinked_review_output(tmp_path: Path) -> None:
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    output_file = node_dir / "exec_executor_0_round1.md"
    try:
        output_file.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    executor = ExecutorRoundArtifact(
        provider=ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
        task_id="exec_executor_0",
        content="outside",
        output_file=output_file,
        audit_round_num=None,
        round_num=1,
    )

    with pytest.raises(ValueError, match="safe regular file"):
        build_review_loop_status_payload(
            "review.node",
            node_dir,
            ReviewLoopProgress(latest_executor_outputs=[executor]),
        )


def test_status_payload_rejects_output_changed_after_bound_publication(
    tmp_path: Path,
) -> None:
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    output_file = node_dir / "exec_executor_0_round1.md"
    output_file.write_text("trusted candidate", encoding="utf-8")
    executor = ExecutorRoundArtifact(
        provider=ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
        task_id="exec_executor_0",
        content="trusted candidate",
        output_file=output_file,
        audit_round_num=None,
        round_num=1,
        output_signature=_file_signature(output_file),
    )
    output_file.write_text("substituted candidate", encoding="utf-8")

    with pytest.raises(ValueError, match="bound runtime publication"):
        build_review_loop_status_payload(
            "review.node",
            node_dir,
            ReviewLoopProgress(latest_executor_outputs=[executor]),
        )


def test_persist_review_loop_status_uses_deterministic_sorted_json(
    tmp_path: Path,
) -> None:
    node_dir = tmp_path / "node"
    payload = {
        "node_id": "review.node",
        "executed_audit_rounds": 1,
        "attempted_local_round_num": 1,
        "final_local_round_num": 1,
        "consensus_reached": True,
        "continued_after_consensus_exhaustion": False,
        "invalid_candidate_round_count": 0,
        "no_progress_round_count": 0,
        "artifact_drift_warning_count": 0,
        "canonical_executor_outputs": [],
        "reviewer_outputs": [],
    }

    status_path = persist_review_loop_status(node_dir, payload)

    assert status_path == node_dir / "review-state" / "review-loop-status.json"
    assert (
        status_path.read_text(encoding="utf-8")
        == json.dumps(
            payload,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def test_persist_review_loop_status_rejects_symlinked_review_state_directory(
    tmp_path: Path,
) -> None:
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    review_state_dir = node_dir / "review-state"
    try:
        review_state_dir.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="real directory"):
        persist_review_loop_status(
            node_dir,
            {
                "node_id": "review.node",
                "executed_audit_rounds": 1,
                "attempted_local_round_num": 1,
                "final_local_round_num": 1,
                "consensus_reached": True,
                "continued_after_consensus_exhaustion": False,
                "invalid_candidate_round_count": 0,
                "no_progress_round_count": 0,
                "artifact_drift_warning_count": 0,
                "canonical_executor_outputs": [],
                "reviewer_outputs": [],
            },
        )


def test_empty_later_audit_clears_stale_reviewer_evidence(tmp_path: Path) -> None:
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    progress = ReviewLoopProgress(
        latest_reviewer_outputs=[_reviewer_artifact(node_dir)]
    )

    progress.record_audit_result(
        AuditRoundResult(
            consensus_reached=False,
            clean_fresh_approval=False,
            latest_executor_outputs=None,
            latest_reviewer_outputs=[],
            invalid_candidate_round_count=1,
            no_progress_round_count=0,
            artifact_drift_warning_count=0,
            last_round_num=2,
        )
    )

    payload = build_review_loop_status_payload("review.node", node_dir, progress)

    assert payload["reviewer_outputs"] == []
