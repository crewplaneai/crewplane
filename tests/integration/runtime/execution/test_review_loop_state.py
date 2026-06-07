import json
from pathlib import Path

from orchestrator_cli.core.workflow_models import ProviderSpec
from orchestrator_cli.runtime.execution.consensus import (
    ParsedReviewResult,
    evaluate_review_output,
    render_review_contract,
)
from orchestrator_cli.runtime.execution.review_loop.state import (
    build_review_loop_status_payload,
    persist_review_loop_status,
)
from orchestrator_cli.runtime.execution.review_loop.types import (
    ExecutorRoundArtifact,
    ReviewerRoundArtifact,
    ReviewLoopProgress,
)


def _reviewer_artifact(node_dir: Path) -> ReviewerRoundArtifact:
    output_file = node_dir / "review_reviewer_0_round1.md"
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
        provider=ProviderSpec(provider="review", role="reviewer"),
        task_id="review_reviewer_0",
        evaluation=evaluation,
        output_file=output_file,
    )


def test_status_payload_shape_and_paths_are_relative_to_node_dir(
    tmp_path: Path,
) -> None:
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    executor = ExecutorRoundArtifact(
        provider=ProviderSpec(provider="exec", role="executor"),
        task_id="exec_executor_0",
        content="Candidate",
        output_file=node_dir / "exec_executor_0_round1.md",
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
        }
    ]
    assert payload["reviewer_outputs"][0]["path"] == "review_reviewer_0_round1.md"


def test_persist_review_loop_status_uses_deterministic_sorted_json(
    tmp_path: Path,
) -> None:
    node_dir = tmp_path / "node"
    payload = {
        "node_id": "review.node",
        "executed_audit_rounds": 1,
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
    assert status_path.read_text(encoding="utf-8") == json.dumps(
        payload,
        indent=2,
        sort_keys=True,
    )
