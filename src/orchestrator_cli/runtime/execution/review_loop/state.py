from __future__ import annotations

import json
from pathlib import Path

from orchestrator_cli.artifacts import safe_artifact_name
from orchestrator_cli.core.review_contract import REQUIRED_EMPTY_SENTINEL

from ..consensus import EvaluatedReviewResult
from .types import (
    REVIEW_LOOP_STATUS_FILE,
    ExecutorRoundArtifact,
    ReviewerRoundArtifact,
    ReviewLoopProgress,
    ReviewLoopStatusOutputEntry,
    ReviewLoopStatusPayload,
)


def _review_metadata_path(output_file: Path) -> Path:
    return output_file.with_suffix(".review.json")


def _review_raw_output_path(output_file: Path) -> Path:
    return output_file.with_suffix(".raw.txt")


def _review_state_dir(artifact_dir: Path) -> Path:
    return artifact_dir / "review-state"


def _write_review_state_file(
    artifact_dir: Path,
    file_name: str,
    content: str,
) -> Path:
    review_state_dir = _review_state_dir(artifact_dir)
    review_state_dir.mkdir(parents=True, exist_ok=True)
    file_path = review_state_dir / file_name
    file_path.write_text(content, encoding="utf-8")
    return file_path


def review_loop_status_path(node_dir: Path) -> Path:
    return _review_state_dir(node_dir) / REVIEW_LOOP_STATUS_FILE


def persist_review_evaluation(
    output_file: Path,
    evaluation: EvaluatedReviewResult,
) -> None:
    raw_output_path = _review_raw_output_path(output_file)
    metadata_path = _review_metadata_path(output_file)
    raw_output_path.write_text(evaluation.raw_text, encoding="utf-8")
    metadata_path.write_text(
        json.dumps(evaluation.to_metadata_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    output_file.write_text(evaluation.normalized_markdown, encoding="utf-8")


def _has_unresolved_review_issues(evaluation: EvaluatedReviewResult) -> bool:
    return (
        evaluation.major_issues != REQUIRED_EMPTY_SENTINEL
        or evaluation.minor_issues != REQUIRED_EMPTY_SENTINEL
    )


def render_unresolved_review_packet(
    reviewer_outputs: list[ReviewerRoundArtifact],
) -> str | None:
    sections: list[str] = []
    for artifact in reviewer_outputs:
        evaluation = artifact.evaluation
        if not _has_unresolved_review_issues(evaluation):
            continue
        sections.extend(
            [
                f"### {artifact.provider.provider} reviewer ({artifact.task_id})",
                f"Source artifact: {artifact.output_file.name}",
                "",
            ]
        )
        if evaluation.major_issues != REQUIRED_EMPTY_SENTINEL:
            sections.extend(
                [
                    "#### Major Issues",
                    evaluation.major_issues,
                    "",
                ]
            )
        if evaluation.minor_issues != REQUIRED_EMPTY_SENTINEL:
            sections.extend(
                [
                    "#### Minor Issues",
                    evaluation.minor_issues,
                    "",
                ]
            )
    if not sections:
        return None
    return "\n".join(["## Unresolved Issues", "", *sections]).strip()


def persist_review_state(
    artifact_dir: Path,
    audit_round_num: int | None,
    round_num: int,
    reviewer_output: ReviewerRoundArtifact,
) -> Path:
    state_file_name = (
        f"{safe_artifact_name(reviewer_output.task_id)}-round-{round_num}.state.json"
    )
    raw_output_path = _review_raw_output_path(reviewer_output.output_file)
    metadata_path = _review_metadata_path(reviewer_output.output_file)
    payload = {
        "reviewer": reviewer_output.provider.provider,
        "task_id": reviewer_output.task_id,
        "audit_round_num": audit_round_num,
        "round_num": round_num,
        "approved": reviewer_output.evaluation.approved,
        "verdict": reviewer_output.evaluation.verdict,
        "evaluation_kind": reviewer_output.evaluation.evaluation_kind,
        "original_verdict": reviewer_output.evaluation.original_verdict,
        "had_leading_text": reviewer_output.evaluation.had_leading_text,
        "had_trailing_text": reviewer_output.evaluation.had_trailing_text,
        "major_issues": reviewer_output.evaluation.major_issues,
        "minor_issues": reviewer_output.evaluation.minor_issues,
        "nitpicks": reviewer_output.evaluation.nitpicks,
        "unresolved_fingerprints": list(
            reviewer_output.evaluation.unresolved_fingerprints
        ),
        "unresolved_issue_count": reviewer_output.evaluation.unresolved_issue_count,
        "warnings": list(reviewer_output.evaluation.warnings),
        "normalized_output_artifact": reviewer_output.output_file.name,
        "raw_output_artifact": raw_output_path.name,
        "metadata_artifact": metadata_path.name,
    }
    return _write_review_state_file(
        artifact_dir=artifact_dir,
        file_name=state_file_name,
        content=json.dumps(payload, indent=2, sort_keys=True),
    )


def render_review_inbox(
    node_id: str,
    audit_round_num: int | None,
    round_num: int,
    executor_outputs: list[ExecutorRoundArtifact],
    previous_executor_outputs: list[ExecutorRoundArtifact] | None,
    reviewer_outputs: list[ReviewerRoundArtifact],
) -> str | None:
    unresolved_review_packet = render_unresolved_review_packet(reviewer_outputs)
    if unresolved_review_packet is None:
        return None

    previous_output_by_task = {
        artifact.task_id: artifact for artifact in previous_executor_outputs or []
    }
    if audit_round_num is None:
        heading = f"# Review Inbox: {node_id} round {round_num}"
    else:
        heading = (
            f"# Review Inbox: {node_id} audit round {audit_round_num} "
            f"local round {round_num}"
        )
    sections = [
        heading,
        "",
        unresolved_review_packet,
        "",
        "## Executor Context",
        "",
    ]
    for artifact in executor_outputs:
        sections.extend(
            [
                f"### {artifact.task_id}",
                f"- current-output: {artifact.output_file}",
            ]
        )
        previous_output = previous_output_by_task.get(artifact.task_id)
        if previous_output is not None:
            sections.append(f"- previous-output: {previous_output.output_file}")
        sections.append("")
    sections.extend(
        [
            "## Round Goal",
            (
                "Address unresolved major and minor issues. Nitpicks are optional "
                "unless they hide a correctness problem."
            ),
        ]
    )
    return "\n".join(sections)


def persist_review_inbox(
    artifact_dir: Path,
    round_num: int,
    inbox_markdown: str,
) -> Path:
    return _write_review_state_file(
        artifact_dir=artifact_dir,
        file_name=f"review-inbox-round-{round_num}.md",
        content=inbox_markdown + "\n",
    )


def _status_output_entry(
    node_dir: Path,
    artifact: ExecutorRoundArtifact | ReviewerRoundArtifact,
    role: str,
) -> ReviewLoopStatusOutputEntry:
    return {
        "task_id": artifact.task_id,
        "provider": artifact.provider.provider,
        "role": role,
        "path": str(artifact.output_file.relative_to(node_dir)),
    }


def build_review_loop_status_payload(
    node_id: str,
    node_dir: Path,
    progress: ReviewLoopProgress,
) -> ReviewLoopStatusPayload:
    return {
        "node_id": node_id,
        "executed_audit_rounds": progress.executed_audit_rounds,
        "final_local_round_num": progress.last_round_num,
        "consensus_reached": progress.consensus_reached,
        "continued_after_consensus_exhaustion": progress.continued_after_exhaustion,
        "invalid_candidate_round_count": progress.invalid_candidate_round_count,
        "no_progress_round_count": progress.no_progress_round_count,
        "artifact_drift_warning_count": progress.artifact_drift_warning_count,
        "canonical_executor_outputs": [
            _status_output_entry(node_dir, artifact, "executor")
            for artifact in progress.latest_executor_outputs or []
        ],
        "reviewer_outputs": [
            _status_output_entry(node_dir, artifact, "reviewer")
            for artifact in progress.latest_reviewer_outputs
        ],
    }


def persist_review_loop_status(
    node_dir: Path,
    payload: ReviewLoopStatusPayload,
) -> Path:
    review_state_dir = _review_state_dir(node_dir)
    review_state_dir.mkdir(parents=True, exist_ok=True)
    status_path = review_loop_status_path(node_dir)
    status_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return status_path
