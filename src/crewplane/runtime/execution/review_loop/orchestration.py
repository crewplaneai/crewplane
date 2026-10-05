from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import NoReturn

from crewplane.architecture.contracts import AgentInvoker
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.results.findings import FindingsExtractionError
from crewplane.artifacts.results.review_loop_status import ReviewLoopStopReason
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import PreflightExecutionNode, ProviderRecord
from crewplane.core.review_checkpoint_state import CheckpointPhase
from crewplane.core.workflow.keywords import ProviderRole

from ..common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    execution_console,
    resolve_prompt_with_output_budget_details,
    should_print_console,
)
from ..errors import NodeExecutionError
from . import completion_policy as _completion_policy
from . import initial_candidate as _initial_candidate
from .audit_io import complete_audit_io
from .checkpoint import (
    close_checkpoint,
    commit_checkpoint,
    restore_diagnostics,
    validate_final_findings,
)
from .checkpoint_progress import ProgressRestorer
from .policy import (
    audit_round_context,
    audit_round_dir,
    consensus_failure_allows_continuation,
    resolve_audit_rounds,
    resolve_remediation_depth,
    review_loop_can_finish,
    split_sequential_review_loop_providers,
)
from .rounds import execute_single_audit_round
from .state import (
    build_review_loop_status_payload,
    persist_review_loop_status,
)
from .types import (
    AuditRoundProgress,
    AuditRoundRequest,
    AuditRoundResult,
    ReviewLoopProgress,
    ReviewLoopRunContext,
)
from .validation import validate_executor_outputs
from .workspace_state_paths import discard_executor_workspace_lineage

resolve_reviewer_prompt_context = _initial_candidate.resolve_reviewer_prompt_context
seed_executor_outputs = _initial_candidate.seed_executor_outputs
ReviewPolicyError = _completion_policy.ReviewPolicyError

# Failure-policy case map:
# 1. Executor invocation failure: normal invocation failure path.
# 2. Empty or missing executor output: invalid canonical candidate.
# 3. Redirect-only executor output: invalid canonical candidate.
# 4. Mixed commentary plus a real candidate: accepted by conservative validation.
# 5. Short or oddly formatted but plausible candidate: accepted for reviewer judgment.
# 6. Unchanged remediation candidate: reviewer skipped as no-progress.
# 7. Claimed fixes without real semantic progress: reviewer runs unless candidate is unchanged.
# 8. Repeated unresolved issues after a changed candidate: stall diagnostics only.
# 9. Prior stage artifacts mutated inside the node stage tree: warning-level drift.
# 10. Reserved run-root artifact mutation: fatal when attributable to this invocation.
# 11. Any invalid executor in a multi-executor round: the whole candidate set is invalid.
# 12. Fresh audits re-review the seeded candidate without inherited unresolved state.
# 13. Reviewer invocation failure: normal invocation failure path.
# 14. Malformed reviewer output with content: preserved as unstructured feedback.
# 15. Reviewer mutation of node-local artifacts or review-state: warning-level drift.
# 16. Reviewer mutation of reserved run-root artifacts: fatal when attributable.
# 17. Consensus exhaustion with a valid candidate: persist status and apply continuation policy.
# 18. No valid canonical candidate across all audits: hard failure.
# In attributable windows, the event log may only gain records emitted by this
# runtime invocation; concurrent node windows only reject destructive event-log drift.


async def execute_review_loop_stage(
    stage: PreflightExecutionNode,
    output: ArtifactStorePort,
    node_dir: Path,
    runtime_context: CompiledRuntimeContext,
    invoker: AgentInvoker,
    telemetry: ExecutionTelemetry | None,
) -> None:
    """Resume or run a sequential review loop and commit its final checkpoint.

    Preparation errors propagate before failure status handling begins. Settled
    policy errors close partial reuse; other failures preserve the checkpoint and
    publish failure status. Cancellation publishes cancelled status only after
    outstanding publication work drains. The caller owns runtime resources.
    """
    providers = split_sequential_review_loop_providers(
        stage.id,
        stage.provider_records,
    )
    progress = await complete_audit_io(
        partial(_restore_review_loop_progress, stage, output, runtime_context)
    )
    if progress.next_phase == "finalize":
        await complete_audit_io(
            partial(restore_diagnostics, node_dir, stage.id, progress)
        )
        return
    context = await complete_audit_io(
        partial(
            _build_review_loop_context,
            stage,
            output,
            node_dir,
            runtime_context,
            invoker,
            telemetry,
            providers,
        )
    )
    try:
        await execute_review_loop_audits(context, progress)
        await complete_audit_io(partial(_validate_final_findings, context, progress))
        await _commit_transition(context, progress, "finalize", progress.last_round_num)
    except ReviewPolicyError as exc:
        await complete_audit_io(partial(_reject_review_loop, context, exc))
    except asyncio.CancelledError as exc:
        progress.stop_reason = ReviewLoopStopReason.CANCELLED
        progress.consensus_reached = False
        await complete_audit_io(
            partial(_publish_review_loop_failure, context, progress, exc)
        )
    except Exception as exc:
        if progress.stop_reason is None:
            progress.stop_reason = ReviewLoopStopReason.FAILED
        await complete_audit_io(
            partial(_publish_review_loop_failure, context, progress, exc)
        )


def _restore_review_loop_progress(
    stage: PreflightExecutionNode,
    output: ArtifactStorePort,
    runtime_context: CompiledRuntimeContext,
) -> ReviewLoopProgress:
    checkpoint = runtime_context.review_checkpoints.get(stage.id)
    if checkpoint is None:
        return ReviewLoopProgress()
    return ProgressRestorer(
        checkpoint,
        output.stages_dir,
        stage.provider_records,
        resolve_audit_rounds(stage),
    ).restore()


def _build_review_loop_context(
    stage: PreflightExecutionNode,
    output: ArtifactStorePort,
    node_dir: Path,
    runtime_context: CompiledRuntimeContext,
    invoker: AgentInvoker,
    telemetry: ExecutionTelemetry | None,
    providers: tuple[list[ProviderRecord], list[ProviderRecord]],
) -> ReviewLoopRunContext:
    executors, reviewers = providers
    executor_prompt = resolve_prompt_with_output_budget_details(
        runtime_context,
        stage,
        output,
        role=ProviderRole.EXECUTOR,
        telemetry=telemetry,
    )
    reviewer_prompt_context = resolve_reviewer_prompt_context(
        runtime_context,
        stage,
        output,
        telemetry,
    )
    return ReviewLoopRunContext(
        runtime_context=runtime_context,
        stage=stage,
        output=output,
        node_dir=node_dir,
        invoker=invoker,
        telemetry=telemetry,
        executors=tuple(executors),
        reviewers=tuple(reviewers),
        executor_prompt=executor_prompt.text,
        executor_prompt_workspace_files=executor_prompt.workspace_files,
        reviewer_prompt_context=reviewer_prompt_context.text,
        reviewer_prompt_workspace_files=reviewer_prompt_context.workspace_files,
        remediation_depth=resolve_remediation_depth(stage),
        audit_rounds=resolve_audit_rounds(stage),
    )


async def execute_review_loop_audits(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
) -> None:
    """Run audits in cursor order and publish status before terminal policy actions.

    Preserve fresh-audit candidate reuse and stop the entire loop on no progress.
    A valid exhausted candidate may continue under policy; missing candidates and
    fatal exhaustion raise ReviewPolicyError after status publication and logging.
    Invocation and storage errors propagate to the stage's failure handling.
    """
    for audit_round_num in range(progress.cursor_audit, context.audit_rounds + 1):
        progress.cursor_audit = audit_round_num
        progress.executed_audit_rounds = audit_round_num
        audit_result = await _execute_review_loop_audit_round(
            context,
            progress,
            audit_round_num,
        )
        if audit_result.stop_reason == ReviewLoopStopReason.NO_PROGRESS:
            await _finish_stalled_audit(context, progress)
            return
        can_finish = review_loop_can_finish(context, audit_result, audit_round_num)
        if (
            audit_round_num == context.audit_rounds
            and not can_finish
            and (
                progress.latest_executor_outputs is None
                or not consensus_failure_allows_continuation(context.stage)[0]
            )
        ):
            await complete_audit_io(
                partial(_publish_final_audit_rejection, context, progress)
            )
            return
        await _publish_review_loop_status(context, progress)
        progress.cursor_round = 1
        progress.next_phase = "executors"
        if can_finish:
            progress.stop_reason = ReviewLoopStopReason.CONSENSUS
            await _publish_review_loop_status(context, progress)
            return

    await _finish_exhausted_review_loop(context, progress)


async def _finish_exhausted_review_loop(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
) -> None:
    should_continue, reason = consensus_failure_allows_continuation(context.stage)
    if progress.latest_executor_outputs is None or not should_continue:
        await complete_audit_io(
            partial(_publish_exhausted_rejection, context, progress)
        )
        return
    progress.mark_consensus_exhausted(continued=True)
    await _publish_review_loop_status(context, progress)
    await complete_audit_io(
        partial(
            _completion_policy.apply_exhausted_review_loop_policy,
            context,
            progress,
            reason,
        )
    )


def _publish_final_audit_rejection(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
) -> None:
    """Drain the final round publication through its settled policy rejection."""
    _persist_review_loop_status(context, progress)
    progress.cursor_round = 1
    progress.next_phase = "executors"
    _publish_exhausted_rejection(context, progress)


def _publish_exhausted_rejection(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
) -> None:
    """Keep publication and settled policy rejection in one draining operation."""
    progress.mark_consensus_exhausted(continued=False)
    if progress.latest_executor_outputs is None:
        progress.stop_reason = ReviewLoopStopReason.NO_VALID_CANDIDATE
    _persist_review_loop_status(context, progress)
    _completion_policy.apply_exhausted_review_loop_policy(context, progress, None)


async def _finish_stalled_audit(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
) -> None:
    if not context.stage.execution_policy.continue_on_failure:
        await complete_audit_io(partial(_publish_stalled_rejection, context, progress))
        return
    await _publish_review_loop_status(context, progress)
    progress.continued_after_stop = True
    await _publish_review_loop_status(context, progress)
    await complete_audit_io(
        partial(_completion_policy.apply_stalled_review_loop_policy, context, progress)
    )


def _publish_stalled_rejection(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
) -> None:
    """Publish round and policy status before propagating settled rejection."""
    _persist_review_loop_status(context, progress)
    finish_stalled_review_loop(context, progress)


def finish_stalled_review_loop(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
) -> None:
    """Synchronously publish a stalled loop's continuation decision and log it.

    Only continue_on_failure permits continuation. Otherwise raise ReviewPolicyError
    after publication and logging; storage and logging errors take precedence.
    Leave checkpoint closure and runtime resource cleanup to the caller.
    """
    continued = context.stage.execution_policy.continue_on_failure
    progress.continued_after_stop = continued
    _persist_review_loop_status(context, progress)
    _completion_policy.apply_stalled_review_loop_policy(context, progress)


async def _execute_review_loop_audit_round(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
    audit_round_num: int,
) -> AuditRoundResult:
    audit_dir = await complete_audit_io(
        partial(
            audit_round_dir, context.node_dir, context.audit_rounds, audit_round_num
        )
    )
    audit_context = audit_round_context(
        context.audit_rounds,
        audit_round_num,
    )
    await complete_audit_io(
        partial(_print_audit_round_header, context, audit_round_num)
    )
    if progress.active_audit is None:
        if audit_round_num > 1 and progress.latest_executor_outputs is None:
            await _commit_transition(context, progress, "executors", 1)
        initial_review_handoff = await _initial_candidate.initial_pre_review_handoff(
            context,
            progress,
            audit_dir,
            audit_context,
            audit_round_num,
            partial(_commit_transition, context, progress),
        )
        progress.last_round_num = 1
        initial_executor_outputs = (
            await _initial_candidate.initial_audit_executor_outputs(
                context,
                progress,
                audit_dir,
                audit_context,
                initial_review_handoff,
            )
        )
        progress.active_audit = AuditRoundProgress(
            executor_outputs=initial_executor_outputs,
            latest_valid_executor_outputs=(
                initial_executor_outputs
                if validate_executor_outputs(initial_executor_outputs).valid
                else None
            ),
            stall=progress.stall,
        )
    audit_result = await execute_single_audit_round(
        AuditRoundRequest(
            runtime_context=context.runtime_context,
            stage=context.stage,
            output=context.output,
            node_dir=context.node_dir,
            invoker=context.invoker,
            telemetry=context.telemetry,
            executors=context.executors,
            reviewers=context.reviewers,
            executor_prompt=context.executor_prompt,
            executor_prompt_workspace_files=context.executor_prompt_workspace_files,
            reviewer_prompt_context=context.reviewer_prompt_context,
            reviewer_prompt_workspace_files=context.reviewer_prompt_workspace_files,
            audit_dir=audit_dir,
            remediation_depth=context.remediation_depth,
            initial_executor_outputs=progress.active_audit.executor_outputs,
            audit_round_num=audit_context,
            progress=progress.active_audit,
            publish_status=partial(_persist_review_loop_status, context, progress),
            commit_transition=partial(_commit_transition, context, progress),
            start_round=progress.cursor_round,
            start_phase=progress.next_phase,
            rejected_invocations=context.rejected_invocations,
        )
    )
    progress.record_audit_result(audit_result)
    return audit_result


def _print_audit_round_header(
    context: ReviewLoopRunContext,
    audit_round_num: int,
) -> None:
    if not should_print_console(context.telemetry):
        return
    if context.audit_rounds > 1:
        execution_console(context.telemetry).print(
            f"\n[bold]Audit round {audit_round_num}/{context.audit_rounds}[/]"
        )
        return
    execution_console(context.telemetry).print(
        f"\n[bold]Review cycle (depth {context.remediation_depth})[/]"
    )


def _persist_review_loop_status(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
) -> Path:
    payload = build_review_loop_status_payload(
        node_id=context.stage.id,
        node_dir=context.node_dir,
        progress=progress,
    )
    publications = context.runtime_context.runtime_publications
    with publications.transaction():
        path = persist_review_loop_status(context.node_dir, payload)
        publications.publish(path, file_size_and_sha256(path), recovery_source=path)
        return path


async def _publish_review_loop_status(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
) -> None:
    await complete_audit_io(partial(_persist_review_loop_status, context, progress))


def _publish_review_loop_failure(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
    error: BaseException,
) -> NoReturn:
    """Publish status before re-raising the original failure ahead of cancellation."""
    _persist_review_loop_status(context, progress)
    raise error


async def _commit_transition(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
    phase: CheckpointPhase,
    local_round: int,
) -> None:
    await commit_checkpoint(context, progress, phase, local_round)
    await complete_audit_io(partial(_discard_rejected_invocations, context))


def _discard_rejected_invocations(context: ReviewLoopRunContext) -> None:
    for audit, local_round, task_ids, reason in context.rejected_invocations:
        discard_executor_workspace_lineage(
            context.output, context.stage, task_ids, audit, local_round, reason
        )
    context.rejected_invocations.clear()


def _validate_final_findings(
    context: ReviewLoopRunContext, progress: ReviewLoopProgress
) -> None:
    """Close invalid findings and raise their execution error before cancellation."""
    try:
        validate_final_findings(context, progress)
    except FindingsExtractionError as exc:
        close_checkpoint(context, str(exc))
        raise NodeExecutionError(str(exc)) from exc


def _reject_review_loop(
    context: ReviewLoopRunContext, error: ReviewPolicyError
) -> NoReturn:
    """Close and discard in order, keeping settled policy errors ahead of cancellation."""
    close_checkpoint(context, str(error))
    _discard_rejected_invocations(context)
    raise error
