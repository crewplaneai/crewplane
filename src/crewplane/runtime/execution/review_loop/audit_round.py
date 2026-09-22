from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Literal

from crewplane.architecture.contracts import LogLevel
from crewplane.architecture.contracts.invocation_failures import InvocationFailureError
from crewplane.artifacts.results.review_loop_status import ReviewLoopStopReason
from crewplane.core.workflow.keywords import ProviderRole

from ..common import (
    RuntimeEventContext,
    emit_runtime_log,
    execution_console,
    resolve_prompt_with_output_budget_details,
    should_print_console,
)
from ..consensus import check_consensus
from ..fragment_assembler import ResolvedPrompt
from ..workspace_files.source_resolution import WorkspaceCandidateSourceContext
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
from .workspace_state_paths import discard_executor_workspace_lineage


class AuditIterationOutcome(Enum):
    """Control whether the audit advances, stops, or returns an approval."""

    CONTINUE = auto()
    STOP = auto()
    CONSENSUS = auto()


@dataclass(frozen=True)
class ReviewCandidate:
    """A candidate ready for review, even when its fingerprint is unavailable."""

    fingerprint: str | None


async def execute_single_audit_round(
    request: AuditRoundRequest,
) -> AuditRoundResult:
    """Execute one fresh-audit plus remediation loop for cases 2-9 and 11-12."""
    progress = request.progress or AuditRoundProgress(
        executor_outputs=request.initial_executor_outputs
    )
    try:
        return await execute_audit_round_iterations(request, progress)
    finally:
        if request.checkpoint is not None:
            request.checkpoint()


async def execute_audit_round_iterations(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
) -> AuditRoundResult:
    """Run audit iterations until consensus, a stop decision, or depth exhaustion."""
    for round_num in range(1, request.remediation_depth + 2):
        progress.last_round_num = round_num
        if request.checkpoint is not None:
            request.checkpoint()
        outcome = await _execute_audit_iteration(request, progress, round_num)
        if outcome is AuditIterationOutcome.STOP:
            break
        if outcome is AuditIterationOutcome.CONSENSUS:
            return progress.to_result(
                consensus_reached=True,
                clean_fresh_approval=round_num == 1,
            )
    return progress.to_result(
        consensus_reached=False,
        clean_fresh_approval=False,
    )


async def _execute_audit_iteration(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    round_num: int,
) -> AuditIterationOutcome:
    try:
        await run_remediation_executor_round(request, progress, round_num)
    except InvocationFailureError as exc:
        if recover_after_remediation_context_exhaustion(
            request, progress, round_num, exc
        ):
            return AuditIterationOutcome.STOP
        raise

    candidate = _prepare_review_candidate(request, progress, round_num)
    if isinstance(candidate, AuditIterationOutcome):
        return candidate
    round_state = await run_review_phase(
        request, progress, candidate.fingerprint, round_num
    )
    emit_review_stall_warning_if_needed(request, progress, round_state, round_num)
    if review_phase_reached_consensus(request, round_state, round_num):
        progress.stall.observe_review(
            candidate.fingerprint, round_state.reviewer_outputs
        )
        return AuditIterationOutcome.CONSENSUS
    progress.advance_review_state(
        current_review_packet=round_state.current_review_packet,
        current_unresolved_fingerprints=round_state.current_unresolved_fingerprints,
        current_executor_fingerprint=round_state.current_executor_fingerprint,
    )
    return AuditIterationOutcome.CONTINUE


def _prepare_review_candidate(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    round_num: int,
) -> (
    ReviewCandidate
    | Literal[AuditIterationOutcome.CONTINUE, AuditIterationOutcome.STOP]
):
    validation = validate_executor_outputs(progress.executor_outputs)
    if not validation.valid:
        if record_invalid_candidate_and_should_stop(
            request, progress, validation, round_num
        ):
            return AuditIterationOutcome.STOP
        return AuditIterationOutcome.CONTINUE
    fingerprint = build_executor_output_fingerprint(progress.executor_outputs)
    if is_no_progress_candidate(progress, fingerprint, round_num):
        record_no_progress_candidate(request, progress, round_num)
        if progress.stall.record_unchanged_attempt():
            progress.stop_reason = ReviewLoopStopReason.NO_PROGRESS
            return AuditIterationOutcome.STOP
        return AuditIterationOutcome.CONTINUE
    if fingerprint is None or fingerprint != progress.stall.candidate_fingerprint:
        progress.stall.consecutive_round_count = 0
    return ReviewCandidate(fingerprint)


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
            executor_prompt_workspace_files=request.executor_prompt_workspace_files,
            previous_review_packet=progress.previous_review_packet,
            previous_executor_outputs=progress.previous_executor_outputs,
            recovery_attempt=progress.stall.consecutive_round_count > 0,
        )
    )
    progress.executor_outputs = executor_run.outputs
    progress.add_artifact_drift_warnings(executor_run.drift_warning_count)


async def run_review_phase(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    current_executor_fingerprint: str | None,
    round_num: int,
) -> ReviewRoundState:
    """Run reviewers and record their completed round before rendering its state."""
    reviewer_run = await run_reviewer_round(
        _build_reviewer_round_request(request, progress, round_num)
    )
    reviewer_outputs = reviewer_run.outputs
    progress.record_completed_review(reviewer_run, round_num)
    persist_round_review_inbox(request, progress, reviewer_outputs, round_num)
    return ReviewRoundState(
        reviewer_outputs=reviewer_outputs,
        reviewer_failure_count=reviewer_run.reviewer_failure_count,
        current_review_packet=render_unresolved_review_packet(reviewer_outputs),
        current_unresolved_fingerprints=collect_unresolved_fingerprints(
            reviewer_outputs
        ),
        current_executor_fingerprint=current_executor_fingerprint,
    )


def _build_reviewer_round_request(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    round_num: int,
) -> ReviewerRoundRequest:
    prompt = _resolve_reviewer_prompt(request, round_num)
    return ReviewerRoundRequest(
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
        reviewer_prompt_context=prompt.text,
        reviewer_prompt_workspace_files=prompt.workspace_files,
        review_context=build_review_context(progress.executor_outputs),
        previous_review_packet=progress.previous_review_packet,
    )


def _resolve_reviewer_prompt(
    request: AuditRoundRequest, round_num: int
) -> ResolvedPrompt:
    if request.reviewer_prompt_context:
        return ResolvedPrompt(
            request.reviewer_prompt_context, request.reviewer_prompt_workspace_files
        )
    return resolve_prompt_with_output_budget_details(
        request.runtime_context,
        request.stage,
        request.output,
        role=ProviderRole.REVIEWER,
        telemetry=request.telemetry,
        workspace_candidate_context=WorkspaceCandidateSourceContext(
            role_label=ProviderRole.REVIEWER,
            round_num=round_num,
            audit_round_num=request.audit_round_num,
        ),
    )


def recover_after_remediation_context_exhaustion(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    round_num: int,
    exc: InvocationFailureError,
) -> bool:
    latest_valid = progress.latest_valid_executor_outputs
    if (
        round_num == 1
        or latest_valid is None
        or exc.kind != "provider_session_context_exhausted"
    ):
        return False
    discard_executor_workspace_lineage(
        request.output,
        request.stage,
        {provider.task_id for provider in request.executors},
        request.audit_round_num,
        round_num,
        "remediation_context_exhausted",
    )
    progress.executor_outputs = latest_valid
    emit_remediation_context_exhaustion_warning(request, round_num, exc)
    return True


def emit_remediation_context_exhaustion_warning(
    request: AuditRoundRequest,
    round_num: int,
    exc: InvocationFailureError,
) -> None:
    emit_runtime_log(
        request.telemetry,
        level=LogLevel.WARNING,
        message=(
            f"Sequential review loop for node '{request.stage.id}' stopped "
            "remediation after provider session context exhaustion. Continuing "
            "with the latest valid candidate."
        ),
        operation="review_loop_remediation_context_exhausted",
        context=RuntimeEventContext(
            node_id=request.stage.id,
            audit_round_num=request.audit_round_num,
            round_num=round_num,
        ),
        attributes={
            "failure_kind": exc.kind,
            "failure_phase": exc.phase,
            "failure_source": exc.source,
        },
    )


def record_invalid_candidate_and_should_stop(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    validation: CandidateValidationResult,
    round_num: int,
) -> bool:
    progress.record_invalid_candidate()
    progress.stall.consecutive_round_count = 0
    discard_executor_workspace_lineage(
        request.output,
        request.stage,
        {artifact.task_id for artifact in progress.executor_outputs},
        request.audit_round_num,
        round_num,
        validation.reason or "invalid_candidate",
    )
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
    discard_executor_workspace_lineage(
        request.output,
        request.stage,
        {artifact.task_id for artifact in progress.executor_outputs},
        request.audit_round_num,
        round_num,
        "no_progress_candidate",
    )
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
    if round_state.reviewer_failure_count > 0:
        return False
    if not check_consensus(
        [artifact.evaluation for artifact in round_state.reviewer_outputs]
    ):
        return False
    if should_print_console(request.telemetry):
        execution_console(request.telemetry).print(
            f"[green bold]Consensus reached in round {round_num}![/]"
        )
    return True
