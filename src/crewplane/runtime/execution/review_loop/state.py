from __future__ import annotations

import json
from pathlib import Path

from crewplane.architecture.safe_files import (
    contained_regular_file,
    ensure_contained_directory,
)
from crewplane.artifacts import safe_artifact_name
from crewplane.artifacts.atomic import atomic_write_json, atomic_write_text
from crewplane.artifacts.results.review_loop_status import (
    REVIEW_LOOP_STATUS_RELATIVE_PATH,
    ReviewLoopStatusOutputEntry,
    ReviewLoopStatusPayload,
    review_loop_status_path,
)
from crewplane.core.review_contract import REQUIRED_EMPTY_SENTINEL
from crewplane.core.workflow.keywords import ProviderRole

from ..consensus import EvaluatedReviewResult
from ..provider_call import read_bound_invocation_output
from .types import (
    ExecutorRoundArtifact,
    ReviewerInvocationFailure,
    ReviewerRoundArtifact,
    ReviewLoopProgress,
)


def _review_metadata_path(output_file: Path) -> Path:
    return output_file.with_suffix(".review.json")


def _review_raw_output_path(output_file: Path) -> Path:
    return output_file.with_suffix(".raw.txt")


def _write_review_state_file(
    artifact_dir: Path,
    file_name: str,
    content: str,
) -> Path:
    review_state_dir = ensure_contained_directory(artifact_dir, "review-state")
    file_path = review_state_dir / file_name
    return atomic_write_text(file_path, content)


def persist_review_evaluation_artifacts(
    output_file: Path,
    evaluation: EvaluatedReviewResult,
) -> None:
    raw_output_path = _review_raw_output_path(output_file)
    metadata_path = _review_metadata_path(output_file)
    atomic_write_text(raw_output_path, evaluation.raw_text)
    atomic_write_json(
        metadata_path,
        evaluation.to_metadata_dict(),
    )


def _has_unresolved_review_issues(evaluation: EvaluatedReviewResult) -> bool:
    return (
        evaluation.major_issues != REQUIRED_EMPTY_SENTINEL
        or evaluation.minor_issues != REQUIRED_EMPTY_SENTINEL
    )


def _has_feedback_for_executor(evaluation: EvaluatedReviewResult) -> bool:
    return (
        _has_unresolved_review_issues(evaluation)
        or evaluation.unstructured_feedback is not None
    )


def render_unresolved_review_packet(
    reviewer_outputs: list[ReviewerRoundArtifact],
) -> str | None:
    sections: list[str] = []
    for artifact in reviewer_outputs:
        evaluation = artifact.evaluation
        if not _has_feedback_for_executor(evaluation):
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
        if evaluation.unstructured_feedback is not None:
            sections.extend(
                [
                    "#### Unstructured Feedback",
                    (
                        "The runtime could not normalize this reviewer response into "
                        "Major Issues, Minor Issues, or Nitpicks. Treat this as raw "
                        "reviewer feedback, not as normalized candidate findings."
                    ),
                    "",
                    evaluation.unstructured_feedback,
                    "",
                ]
            )
    if not sections:
        return None
    return "\n".join(["## Reviewer Feedback", "", *sections]).strip()


def persist_review_state(
    artifact_dir: Path,
    audit_round_num: int | None,
    round_num: int,
    reviewer_output: ReviewerRoundArtifact,
) -> Path:
    raw_output_path = _review_raw_output_path(reviewer_output.output_file)
    metadata_path = _review_metadata_path(reviewer_output.output_file)
    payload: dict[str, object] = {
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
        "unstructured_feedback": reviewer_output.evaluation.unstructured_feedback,
        "unresolved_fingerprints": list(
            reviewer_output.evaluation.unresolved_fingerprints
        ),
        "unresolved_issue_count": reviewer_output.evaluation.unresolved_issue_count,
        "warnings": list(reviewer_output.evaluation.warnings),
        "normalized_output_artifact": reviewer_output.output_file.name,
        "raw_output_artifact": raw_output_path.name,
        "metadata_artifact": metadata_path.name,
    }
    return _write_reviewer_state(
        reviewer_output.task_id, round_num, artifact_dir, payload
    )


def persist_reviewer_failure_state(
    artifact_dir: Path,
    audit_round_num: int | None,
    round_num: int,
    failure: ReviewerInvocationFailure,
) -> Path:
    payload: dict[str, object] = {
        "reviewer": failure.provider.provider,
        "task_id": failure.task_id,
        "audit_round_num": audit_round_num,
        "round_num": round_num,
        "approved": False,
        "evaluation_kind": "reviewer_failure",
        "failure_kind": failure.failure_kind,
        "failure_message": str(failure.error),
        "output_artifact": (
            failure.output_file.name if failure.output_file.exists() else None
        ),
        "warnings": [failure.warning],
    }
    return _write_reviewer_state(failure.task_id, round_num, artifact_dir, payload)


def _write_reviewer_state(
    task_id: str,
    round_num: int,
    artifact_dir: Path,
    payload: dict[str, object],
) -> Path:
    return _write_review_state_file(
        artifact_dir=artifact_dir,
        file_name=f"{safe_artifact_name(task_id)}-round-{round_num}.state.json",
        content=json.dumps(payload, allow_nan=False, indent=2, sort_keys=True),
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
    role: ProviderRole,
) -> ReviewLoopStatusOutputEntry:
    try:
        relative_path = artifact.output_file.relative_to(node_dir).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"Review output for task '{artifact.task_id}' is outside its node stage."
        ) from exc
    safe_output = contained_regular_file(node_dir, relative_path)
    if safe_output is None:
        raise ValueError(
            f"Review output for task '{artifact.task_id}' is not a safe regular file."
        )
    if artifact.output_signature is None:
        raise ValueError(
            f"Review output for task '{artifact.task_id}' has no bound runtime "
            "publication."
        )
    try:
        read_bound_invocation_output(safe_output, artifact.output_signature)
    except RuntimeError as exc:
        raise ValueError(
            f"Review output for task '{artifact.task_id}' does not match its "
            "bound runtime publication."
        ) from exc
    size_bytes, sha256 = artifact.output_signature
    return {
        "task_id": artifact.task_id,
        "provider": artifact.provider.provider,
        "role": role,
        "path": relative_path,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "audit_round_num": artifact.audit_round_num,
        "round_num": artifact.round_num,
    }


def build_review_loop_status_payload(
    node_id: str,
    node_dir: Path,
    progress: ReviewLoopProgress,
) -> ReviewLoopStatusPayload:
    selected_round_num = progress.selected_round_num or _selected_round_num(progress)
    return {
        "node_id": node_id,
        "executed_audit_rounds": progress.executed_audit_rounds,
        "attempted_local_round_num": progress.last_round_num,
        "final_local_round_num": selected_round_num,
        "consensus_reached": progress.consensus_reached,
        "continued_after_consensus_exhaustion": progress.continued_after_exhaustion,
        "invalid_candidate_round_count": progress.invalid_candidate_round_count,
        "no_progress_round_count": progress.no_progress_round_count,
        "artifact_drift_warning_count": progress.artifact_drift_warning_count,
        "canonical_executor_outputs": [
            _status_output_entry(node_dir, artifact, ProviderRole.EXECUTOR)
            for artifact in progress.latest_executor_outputs or []
        ],
        "reviewer_outputs": [
            _status_output_entry(node_dir, artifact, ProviderRole.REVIEWER)
            for artifact in progress.latest_reviewer_outputs
        ],
    }


def _selected_round_num(progress: ReviewLoopProgress) -> int:
    if progress.latest_executor_outputs:
        return progress.latest_executor_outputs[0].round_num
    if progress.latest_reviewer_outputs:
        return progress.latest_reviewer_outputs[0].round_num
    return 0


def persist_review_loop_status(
    node_dir: Path,
    payload: ReviewLoopStatusPayload,
) -> Path:
    ensure_contained_directory(
        node_dir, REVIEW_LOOP_STATUS_RELATIVE_PATH.parent.as_posix()
    )
    status_path = review_loop_status_path(node_dir)
    return atomic_write_json(status_path, payload)
