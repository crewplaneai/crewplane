from __future__ import annotations

from ..common import execution_console, should_print_console
from ..consensus import check_consensus
from .executor_round import run_executor_round
from .prompts import build_review_context
from .reviewer_round import run_reviewer_round
from .state import (
    persist_review_inbox,
    render_review_inbox,
    render_unresolved_review_packet,
)
from .types import (
    AuditRoundProgress,
    AuditRoundRequest,
    AuditRoundResult,
    CandidateValidationResult,
    ExecutorRoundRequest,
    ReviewerRoundArtifact,
    ReviewerRoundRequest,
    ReviewRoundState,
)
from .validation import (
    build_executor_output_fingerprint,
    collect_unresolved_fingerprints,
    count_unresolved_review_issues,
    emit_invalid_candidate_warning,
    emit_no_progress_warning,
    emit_review_stall_warning,
    is_no_progress_candidate,
    validate_executor_outputs,
)


async def execute_single_audit_round(
    request: AuditRoundRequest,
) -> AuditRoundResult:
    """Execute one fresh-audit plus remediation loop for cases 2-9 and 11-12."""
    progress = AuditRoundProgress(executor_outputs=request.initial_executor_outputs)

    for round_num in range(1, request.remediation_depth + 2):
        progress.last_round_num = round_num
        await run_remediation_executor_round(request, progress, round_num)

        validation = validate_executor_outputs(progress.executor_outputs)
        if not validation.valid:
            if record_invalid_candidate_and_should_stop(
                request,
                progress,
                validation,
                round_num,
            ):
                break
            continue

        current_executor_fingerprint = build_executor_output_fingerprint(
            progress.executor_outputs
        )
        if is_no_progress_candidate(progress, current_executor_fingerprint, round_num):
            record_no_progress_candidate(request, progress, round_num)
            continue

        round_state = await run_review_phase(
            request,
            progress,
            current_executor_fingerprint,
            round_num,
        )
        emit_review_stall_warning_if_needed(request, progress, round_state, round_num)

        if review_phase_reached_consensus(request, round_state, round_num):
            return progress.to_result(
                consensus_reached=True,
                clean_fresh_approval=round_num == 1,
            )

        progress.advance_review_state(
            current_review_packet=round_state.current_review_packet,
            current_unresolved_fingerprints=round_state.current_unresolved_fingerprints,
            current_executor_fingerprint=round_state.current_executor_fingerprint,
        )

    return progress.to_result(
        consensus_reached=False,
        clean_fresh_approval=False,
    )


async def run_remediation_executor_round(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    round_num: int,
) -> None:
    if round_num == 1:
        return
    executor_run = await run_executor_round(
        ExecutorRoundRequest(
            runtime_context=request.runtime_context,
            node=request.stage,
            output=request.output,
            node_dir=request.node_dir,
            invoker=request.invoker,
            telemetry=request.telemetry,
            executors=request.executors,
            audit_round_num=request.audit_round_num,
            round_num=round_num,
            artifact_dir=request.audit_dir,
            executor_prompt=request.executor_prompt,
            previous_review_packet=progress.previous_review_packet,
            previous_executor_outputs=progress.previous_executor_outputs,
        )
    )
    progress.executor_outputs = executor_run.outputs
    progress.add_artifact_drift_warnings(executor_run.drift_warning_count)


async def run_review_phase(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    current_executor_fingerprint: str,
    round_num: int,
) -> ReviewRoundState:
    progress.latest_valid_executor_outputs = progress.executor_outputs
    reviewer_run = await run_reviewer_round(
        ReviewerRoundRequest(
            runtime_context=request.runtime_context,
            node=request.stage,
            output=request.output,
            node_dir=request.node_dir,
            invoker=request.invoker,
            telemetry=request.telemetry,
            reviewers=request.reviewers,
            audit_round_num=request.audit_round_num,
            round_num=round_num,
            artifact_dir=request.audit_dir,
            reviewer_prompt_context=request.reviewer_prompt_context,
            review_context=build_review_context(progress.executor_outputs),
            previous_review_packet=progress.previous_review_packet,
        )
    )
    reviewer_outputs = reviewer_run.outputs
    progress.add_artifact_drift_warnings(reviewer_run.drift_warning_count)
    progress.record_review_outputs(reviewer_outputs)
    persist_round_review_inbox(request, progress, reviewer_outputs, round_num)
    return ReviewRoundState(
        reviewer_outputs=reviewer_outputs,
        current_review_packet=render_unresolved_review_packet(reviewer_outputs),
        current_unresolved_fingerprints=collect_unresolved_fingerprints(
            reviewer_outputs
        ),
        current_executor_fingerprint=current_executor_fingerprint,
    )


def record_invalid_candidate_and_should_stop(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    validation: CandidateValidationResult,
    round_num: int,
) -> bool:
    progress.record_invalid_candidate()
    emit_invalid_candidate_warning(
        telemetry=request.telemetry,
        node_id=request.stage.id,
        audit_round_num=request.audit_round_num,
        round_num=round_num,
        validation=validation,
    )
    if progress.latest_valid_executor_outputs is None:
        return True
    if round_num == 1:
        return True
    progress.executor_outputs = progress.latest_valid_executor_outputs
    return False


def record_no_progress_candidate(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    round_num: int,
) -> None:
    progress.record_no_progress()
    emit_no_progress_warning(
        telemetry=request.telemetry,
        node_id=request.stage.id,
        audit_round_num=request.audit_round_num,
        round_num=round_num,
    )


def persist_round_review_inbox(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    reviewer_outputs: list[ReviewerRoundArtifact],
    round_num: int,
) -> None:
    inbox_markdown = render_review_inbox(
        node_id=request.stage.id,
        audit_round_num=request.audit_round_num,
        round_num=round_num,
        executor_outputs=progress.executor_outputs,
        previous_executor_outputs=progress.previous_executor_outputs,
        reviewer_outputs=reviewer_outputs,
    )
    if inbox_markdown is not None:
        persist_review_inbox(request.audit_dir, round_num, inbox_markdown)


def emit_review_stall_warning_if_needed(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    round_state: ReviewRoundState,
    round_num: int,
) -> None:
    if progress.previous_executor_fingerprint is None:
        return
    emit_review_stall_warning(
        telemetry=request.telemetry,
        node_id=request.stage.id,
        audit_round_num=request.audit_round_num,
        round_num=round_num,
        previous_unresolved_fingerprints=progress.previous_unresolved_fingerprints,
        current_unresolved_fingerprints=round_state.current_unresolved_fingerprints,
        current_unresolved_issue_count=count_unresolved_review_issues(
            round_state.reviewer_outputs
        ),
        previous_executor_fingerprint=progress.previous_executor_fingerprint,
        current_executor_fingerprint=round_state.current_executor_fingerprint,
    )


def review_phase_reached_consensus(
    request: AuditRoundRequest,
    round_state: ReviewRoundState,
    round_num: int,
) -> bool:
    if not check_consensus(
        [artifact.evaluation for artifact in round_state.reviewer_outputs]
    ):
        return False
    if should_print_console(request.telemetry):
        execution_console(request.telemetry).print(
            f"[green bold]Consensus reached in round {round_num}![/]"
        )
    return True
