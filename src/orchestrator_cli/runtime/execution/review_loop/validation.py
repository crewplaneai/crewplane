from __future__ import annotations

import hashlib
import re
from pathlib import Path

from orchestrator_cli.artifacts.failure_artifacts import (
    is_synthetic_invocation_failure,
)
from orchestrator_cli.core.preflight.models import ProviderRecord

from ..common import ExecutionTelemetry, RuntimeEventContext, emit_runtime_log
from ..consensus import EvaluatedReviewResult
from .types import (
    INVALID_CANDIDATE_EMPTY,
    INVALID_CANDIDATE_REDIRECTED,
    AuditRoundProgress,
    CandidateValidationResult,
    ExecutorRoundArtifact,
    ReviewerRoundArtifact,
)

WHITESPACE_PATTERN = re.compile(r"\s+")
ROUND_ARTIFACT_REFERENCE_PATTERN = re.compile(
    r"(?:^|[`(/<\s])([\w./-]+_round\d+\.md)\b"
)
REDIRECT_SIGNAL_PATTERN = re.compile(
    r"\b(updated?|updating|see|refer(?:ring)?|checking|last pass|in place|"
    r"changes? are in|prior design|current repo shape|progress update|"
    r"applied|implemented|fixed)\b",
    re.IGNORECASE,
)
REDIRECT_LEADING_PHRASE_PATTERN = re.compile(
    r"^(?:i['’]?ve|i have|updated?\b|see\b|refer(?:ring)?\b|check(?:ed|ing)?\b|"
    r"the design update|i['’]?m|i am|the current repo shape|changes? are in\b|"
    r"progress update\b)",
    re.IGNORECASE,
)
STATUS_ONLY_PARAGRAPH_PATTERN = re.compile(
    r"^(?:done|ready for review|no further (?:action|changes)(?: needed|required)?|"
    r"changes? (?:applied|complete|completed)|completed|fixed|implemented|addressed)"
    r"\.?$",
    re.IGNORECASE,
)
WORD_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def collect_unresolved_fingerprints(
    reviewer_outputs: list[ReviewerRoundArtifact],
) -> tuple[str, ...]:
    fingerprints = {
        fingerprint
        for artifact in reviewer_outputs
        for fingerprint in artifact.evaluation.unresolved_fingerprints
    }
    return tuple(sorted(fingerprints))


def count_unresolved_review_issues(
    reviewer_outputs: list[ReviewerRoundArtifact],
) -> int:
    return sum(
        artifact.evaluation.unresolved_issue_count for artifact in reviewer_outputs
    )


def _normalize_candidate_content(content: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", content).strip()


def build_executor_output_fingerprint(
    executor_outputs: list[ExecutorRoundArtifact],
) -> str:
    payload = "\n".join(
        f"{artifact.task_id}\n{_normalize_candidate_content(artifact.content)}"
        for artifact in executor_outputs
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def is_no_progress_candidate(
    progress: AuditRoundProgress,
    current_executor_fingerprint: str,
    round_num: int,
) -> bool:
    return (
        round_num > 1
        and progress.previous_review_packet is not None
        and progress.previous_executor_fingerprint == current_executor_fingerprint
    )


def emit_review_stall_warning(
    telemetry: ExecutionTelemetry | None,
    node_id: str,
    audit_round_num: int | None,
    round_num: int,
    previous_unresolved_fingerprints: tuple[str, ...],
    current_unresolved_fingerprints: tuple[str, ...],
    current_unresolved_issue_count: int,
    previous_executor_fingerprint: str | None,
    current_executor_fingerprint: str,
) -> None:
    repeated_fingerprints = sorted(
        set(previous_unresolved_fingerprints).intersection(
            current_unresolved_fingerprints
        )
    )
    if not repeated_fingerprints:
        return

    executor_output_changed = (
        previous_executor_fingerprint != current_executor_fingerprint
    )
    if executor_output_changed:
        message = (
            f"Sequential review loop for node '{node_id}' repeated unresolved review "
            "state after executor output changed. The applied fixes may not be "
            "addressing the remaining reviewer concerns."
        )
    else:
        message = (
            f"Sequential review loop for node '{node_id}' repeated unresolved review "
            "state without executor output changes. This may indicate disagreement "
            "between executor and reviewer or simple non-progress."
        )
    emit_runtime_log(
        telemetry,
        level="warning",
        message=message,
        operation="review_stall_detection",
        context=RuntimeEventContext(
            node_id=node_id,
            audit_round_num=audit_round_num,
            round_num=round_num,
        ),
        attributes={
            "executor_output_changed": executor_output_changed,
            "repeated_fingerprint_count": len(repeated_fingerprints),
            "current_unresolved_issue_count": current_unresolved_issue_count,
        },
    )


def emit_review_evaluation_warnings(
    telemetry: ExecutionTelemetry | None,
    node_id: str,
    provider: ProviderRecord,
    task_id: str,
    audit_round_num: int | None,
    round_num: int,
    output_file: Path,
    evaluation: EvaluatedReviewResult,
) -> None:
    if not evaluation.warnings:
        return

    context = RuntimeEventContext(
        node_id=node_id,
        provider=provider.provider,
        role="reviewer",
        task_id=task_id,
        audit_round_num=audit_round_num,
        round_num=round_num,
        output_file=output_file,
    )
    attributes = {
        "approved": evaluation.approved,
        "evaluation_kind": evaluation.evaluation_kind,
        "had_leading_text": evaluation.had_leading_text,
        "had_trailing_text": evaluation.had_trailing_text,
        "normalized_verdict": evaluation.verdict,
    }
    if evaluation.original_verdict is not None:
        attributes["original_verdict"] = evaluation.original_verdict
    for warning in evaluation.warnings:
        emit_runtime_log(
            telemetry,
            level="warning",
            message=warning,
            operation="review_output_normalization",
            context=context,
            attributes=attributes,
        )


def _split_candidate_paragraphs(content: str) -> tuple[str, ...]:
    paragraphs: list[str] = []
    current: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line:
            current.append(line)
            continue
        if current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return tuple(paragraphs)


def _looks_like_substantive_candidate_paragraph(paragraph: str) -> bool:
    scrubbed = ROUND_ARTIFACT_REFERENCE_PATTERN.sub(" ", paragraph)
    scrubbed = REDIRECT_LEADING_PHRASE_PATTERN.sub(" ", scrubbed, count=1)
    scrubbed = REDIRECT_SIGNAL_PATTERN.sub(" ", scrubbed)
    tokens = [
        token
        for token in WORD_TOKEN_PATTERN.findall(scrubbed.lower())
        if len(token) >= 4
    ]
    return len(scrubbed.strip()) >= 50 and len(tokens) >= 6


def _looks_like_redirect_paragraph(paragraph: str) -> bool:
    if len(paragraph) > 240:
        return False
    if not ROUND_ARTIFACT_REFERENCE_PATTERN.search(paragraph):
        return False
    normalized = paragraph.strip().lower()
    return bool(
        REDIRECT_SIGNAL_PATTERN.search(paragraph)
        or REDIRECT_LEADING_PHRASE_PATTERN.search(paragraph)
        or normalized.startswith(("see ", "refer ", "check ", "updated "))
    )


def _looks_like_status_only_paragraph(paragraph: str) -> bool:
    if len(paragraph) > 80:
        return False
    return bool(STATUS_ONLY_PARAGRAPH_PATTERN.fullmatch(paragraph.strip()))


def _looks_like_redirected_candidate(content: str) -> bool:
    stripped = content.strip()
    if len(stripped) > 4000 or "```" in stripped:
        return False
    if any(line.lstrip().startswith("#") for line in stripped.splitlines()):
        return False
    paragraphs = _split_candidate_paragraphs(stripped)
    if not paragraphs:
        return False
    if any(
        _looks_like_substantive_candidate_paragraph(paragraph)
        for paragraph in paragraphs
    ):
        return False
    redirect_count = sum(
        _looks_like_redirect_paragraph(paragraph) for paragraph in paragraphs
    )
    if redirect_count == 0:
        return False
    return all(
        _looks_like_redirect_paragraph(paragraph)
        or _looks_like_status_only_paragraph(paragraph)
        for paragraph in paragraphs
    )


def classify_executor_output(content: str) -> str | None:
    if not content.strip() or is_synthetic_invocation_failure(content):
        return INVALID_CANDIDATE_EMPTY
    if _looks_like_redirected_candidate(content):
        return INVALID_CANDIDATE_REDIRECTED
    return None


def validate_executor_outputs(
    executor_outputs: list[ExecutorRoundArtifact],
) -> CandidateValidationResult:
    invalid_reasons: dict[str, list[str]] = {}
    for artifact in executor_outputs:
        reason = classify_executor_output(artifact.content)
        if reason is None:
            continue
        invalid_reasons.setdefault(reason, []).append(artifact.task_id)
    if not invalid_reasons:
        return CandidateValidationResult(valid=True)
    reason = sorted(invalid_reasons)[0]
    return CandidateValidationResult(
        valid=False,
        reason=reason,
        invalid_task_ids=tuple(sorted(invalid_reasons[reason])),
    )


def emit_invalid_candidate_warning(
    telemetry: ExecutionTelemetry | None,
    node_id: str,
    audit_round_num: int | None,
    round_num: int,
    validation: CandidateValidationResult,
) -> None:
    if validation.reason is None:
        return
    emit_runtime_log(
        telemetry,
        level="warning",
        message=(
            f"Sequential review loop for node '{node_id}' skipped reviewer invocation "
            f"because the current round candidate was invalid "
            f"({validation.reason})."
        ),
        operation="review_loop_invalid_candidate",
        context=RuntimeEventContext(
            node_id=node_id,
            audit_round_num=audit_round_num,
            round_num=round_num,
        ),
        attributes={
            "reason": validation.reason,
            "invalid_task_ids": ",".join(validation.invalid_task_ids),
        },
    )


def emit_no_progress_warning(
    telemetry: ExecutionTelemetry | None,
    node_id: str,
    audit_round_num: int | None,
    round_num: int,
) -> None:
    emit_runtime_log(
        telemetry,
        level="warning",
        message=(
            f"Sequential review loop for node '{node_id}' skipped reviewer invocation "
            "because the remediation candidate was unchanged after whitespace "
            "normalization."
        ),
        operation="review_loop_no_progress",
        context=RuntimeEventContext(
            node_id=node_id,
            audit_round_num=audit_round_num,
            round_num=round_num,
        ),
    )
