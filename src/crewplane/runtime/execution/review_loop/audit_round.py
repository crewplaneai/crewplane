from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from functools import partial
from typing import Literal, NoReturn

from crewplane.architecture.contracts import LogLevel
from crewplane.architecture.contracts.invocation_failures import InvocationFailureError
from crewplane.artifacts.results.review_loop_status import ReviewLoopStopReason
from crewplane.core.workflow.keywords import ProviderRole

from ..common import (
    RuntimeEventContext,
    emit_runtime_log,
    resolve_prompt_with_output_budget_details,
)
from ..consensus import check_consensus
from ..fragment_assembler import ResolvedPrompt
from ..workspace_files.source_resolution import WorkspaceCandidateSourceContext
from . import audit_publication as _audit_publication
from . import audit_review_outcome as _audit_review_outcome
from .audit_io import complete_audit_io
from .executor_round import run_executor_round
from .policy import consensus_failure_allows_continuation
from .prompts import build_review_context
from .reviewer_round import run_reviewer_round
from .state import render_unresolved_review_packet
from .types import (
    AuditRoundProgress,
    AuditRoundRequest,
    AuditRoundResult,
    CandidateValidationResult,
    ExecutorRoundRequest,
    ReviewerRoundRequest,
    ReviewRoundState,
)
from .validation import (
    build_executor_output_fingerprint,
    collect_unresolved_fingerprints,
    emit_invalid_candidate_warning,
    emit_no_progress_warning,
    is_no_progress_candidate,
    validate_executor_outputs,
)
from .workspace_state_paths import discard_executor_workspace_lineage

persist_round_review_inbox = _audit_publication.persist_round_review_inbox
emit_review_stall_warning_if_needed = (
    _audit_review_outcome.emit_review_stall_warning_if_needed
)
review_phase_reached_consensus = _audit_review_outcome.review_phase_reached_consensus


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
    """Run a fresh or resumed audit, publishing status on exceptional exits.

    Supplied progress is updated in place; otherwise progress starts from the
    initial outputs. Completed audits return directly to orchestration, which
    publishes status with terminal policy so cancellation cannot bypass rejection.
    A completed final rejection also returns on cancellation when policy is fatal.
    Other failures publish status before propagation, keeping the original error
    ahead of cancellation. Publication errors take precedence.
    """
    progress = request.progress or AuditRoundProgress(
        executor_outputs=request.initial_executor_outputs
    )
    try:
        return await execute_audit_round_iterations(request, progress)
    except BaseException as exc:
        if isinstance(
            exc, asyncio.CancelledError
        ) and _completed_review_requires_failure(request, progress):
            return progress.to_result(
                consensus_reached=False, clean_fresh_approval=False
            )
        if request.publish_status is not None:
            await _audit_publication.complete_audit_publication(
                partial(_publish_audit_failure, request.publish_status, exc)
            )
        raise


def _completed_review_requires_failure(
    request: AuditRoundRequest, progress: AuditRoundProgress
) -> bool:
    return (
        (request.audit_round_num or 1)
        == (request.stage.execution_policy.audit_rounds or 1)
        and progress.last_round_num == request.remediation_depth + 1
        and progress.selected_round_num == progress.last_round_num
        and not consensus_failure_allows_continuation(request.stage)[0]
        and not check_consensus(
            [item.evaluation for item in progress.latest_reviewer_outputs]
        )
    )


def _publish_audit_failure(
    publish: Callable[[], object], error: BaseException
) -> NoReturn:
    publish()
    raise error


async def execute_audit_round_iterations(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
) -> AuditRoundResult:
    """Run audit iterations until consensus, a stop decision, or depth exhaustion."""
    for round_num in range(request.start_round, request.remediation_depth + 2):
        progress.last_round_num = round_num
        if request.publish_status is not None:
            await _audit_publication.complete_audit_publication(request.publish_status)
        outcome = await _execute_audit_iteration(request, progress, round_num)
        if outcome is AuditIterationOutcome.STOP:
            break
        if outcome is AuditIterationOutcome.CONSENSUS:
            return progress.to_result(
                consensus_reached=True,
                clean_fresh_approval=round_num == 1,
            )
        if (
            round_num < request.remediation_depth + 1
            and request.commit_transition is not None
        ):
            await request.commit_transition("executors", round_num + 1)
    return progress.to_result(
        consensus_reached=False,
        clean_fresh_approval=False,
    )


async def _execute_audit_iteration(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    round_num: int,
) -> AuditIterationOutcome:
    candidate = await _acquire_review_candidate(request, progress, round_num)
    if isinstance(candidate, AuditIterationOutcome):
        return candidate
    round_state = await run_review_phase(
        request, progress, candidate.fingerprint, round_num
    )
    await complete_audit_io(
        partial(
            emit_review_stall_warning_if_needed,
            request,
            progress,
            round_state,
            round_num,
        )
    )
    if await complete_audit_io(
        partial(review_phase_reached_consensus, request, round_state, round_num)
    ):
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


async def _acquire_review_candidate(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    round_num: int,
) -> (
    ReviewCandidate
    | Literal[AuditIterationOutcome.CONTINUE, AuditIterationOutcome.STOP]
):
    if round_num == request.start_round and request.start_phase == "reviewers":
        return ReviewCandidate(
            build_executor_output_fingerprint(progress.executor_outputs)
        )
    try:
        await run_remediation_executor_round(request, progress, round_num)
    except InvocationFailureError as exc:

        def recover_or_raise(error: InvocationFailureError) -> None:
            if not recover_after_remediation_context_exhaustion(
                request, progress, round_num, error
            ):
                raise error

        try:
            await complete_audit_io(partial(recover_or_raise, exc))
        except asyncio.CancelledError:
            if (request.audit_round_num or 1) != (
                request.stage.execution_policy.audit_rounds or 1
            ) or consensus_failure_allows_continuation(request.stage)[0]:
                raise
        return AuditIterationOutcome.STOP
    completed_candidate: ReviewCandidate | AuditIterationOutcome | None = None

    def prepare_candidate() -> (
        ReviewCandidate
        | Literal[AuditIterationOutcome.CONTINUE, AuditIterationOutcome.STOP]
    ):
        nonlocal completed_candidate
        completed_candidate = _prepare_review_candidate(request, progress, round_num)
        return completed_candidate

    try:
        candidate = await complete_audit_io(prepare_candidate)
    except asyncio.CancelledError:
        stalled = (
            progress.stop_reason == ReviewLoopStopReason.NO_PROGRESS
            and not request.stage.execution_policy.continue_on_failure
        )
        exhausted = (
            isinstance(completed_candidate, AuditIterationOutcome)
            and (request.audit_round_num or 1)
            == (request.stage.execution_policy.audit_rounds or 1)
            and (
                completed_candidate is AuditIterationOutcome.STOP
                or round_num == request.remediation_depth + 1
            )
            and (
                progress.latest_valid_executor_outputs is None
                or not consensus_failure_allows_continuation(request.stage)[0]
            )
        )
        if stalled or exhausted:
            return AuditIterationOutcome.STOP
        raise
    if isinstance(candidate, ReviewCandidate) and request.commit_transition is not None:
        await request.commit_transition("reviewers", round_num)
    return candidate


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
    """Replace candidate outputs and add drift counts after successful remediation.

    Round one keeps its seeded outputs. Invocation failures propagate without
    updating progress; recovery attempts retain the current stall signal.
    """
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
    """Record completed reviews before publishing the inbox and returning state.

    Failures propagated before the reviewer round returns leave progress
    unchanged. Returned failure records are recorded with completed reviews.
    Inbox errors retain those results and precede previous-round state advances.
    """
    reviewer_request = await complete_audit_io(
        partial(_build_reviewer_round_request, request, progress, round_num)
    )
    reviewer_run = await run_reviewer_round(reviewer_request)
    reviewer_outputs = reviewer_run.outputs
    progress.record_completed_review(reviewer_run, round_num)
    await _audit_publication.complete_audit_publication(
        partial(
            persist_round_review_inbox, request, progress, reviewer_outputs, round_num
        )
    )
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
    """Reject exhausted remediation lineage and restore the last valid outputs.

    Recovery requires a round after one, valid fallback outputs, and a provider
    session context exhaustion failure. Checkpointed lineage rejection is queued.

    Returns:
        True when remediation context exhaustion is recoverable and the audit
        should stop. False leaves progress and lineage untouched for the caller
        to propagate the invocation failure.
    """
    latest_valid = progress.latest_valid_executor_outputs
    if (
        round_num == 1
        or latest_valid is None
        or exc.kind != "provider_session_context_exhausted"
    ):
        return False
    _reject_executor_lineage(
        request,
        {provider.task_id for provider in request.executors},
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
    """Log recoverable context exhaustion with the failed invocation's metadata."""
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
    """Count and reject invalid outputs, reset stalls, and emit their warning.

    Returns:
        True when no valid fallback exists or this is a fresh round. False
        restores the last valid outputs so a later remediation round can retry.
        Lineage rejection is deferred when checkpoint transitions are enabled.
    """
    progress.record_invalid_candidate()
    progress.stall.consecutive_round_count = 0
    _reject_executor_lineage(
        request,
        {artifact.task_id for artifact in progress.executor_outputs},
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
    if progress.latest_valid_executor_outputs is None or round_num == 1:
        return True
    progress.executor_outputs = progress.latest_valid_executor_outputs
    return False


def record_no_progress_candidate(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    round_num: int,
) -> None:
    """Count unchanged outputs, reject lineage, restore valid outputs, and warn.

    Missing valid outputs restore an empty list. Checkpointed lineage rejection
    is queued; the caller owns incrementing the consecutive stall count.
    """
    progress.record_no_progress()
    _reject_executor_lineage(
        request,
        {artifact.task_id for artifact in progress.executor_outputs},
        round_num,
        "no_progress_candidate",
    )
    progress.executor_outputs = progress.latest_valid_executor_outputs or []
    emit_no_progress_warning(
        telemetry=request.telemetry,
        node_id=request.stage.id,
        audit_round_num=request.audit_round_num,
        round_num=round_num,
    )


def _reject_executor_lineage(
    request: AuditRoundRequest, task_ids: set[str], round_num: int, reason: str
) -> None:
    if request.commit_transition is None:
        discard_executor_workspace_lineage(
            request.output,
            request.stage,
            task_ids,
            request.audit_round_num,
            round_num,
            reason,
        )
        return
    request.rejected_invocations.append(
        (request.audit_round_num, round_num, task_ids, reason)
    )
