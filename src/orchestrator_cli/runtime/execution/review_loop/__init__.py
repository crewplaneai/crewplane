from __future__ import annotations

from pathlib import Path

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight.models import PreflightExecutionNode
from orchestrator_cli.runtime.agent.types import AgentInvoker

from ..common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    RuntimeEventContext,
    emit_runtime_log,
    execution_console,
    resolve_prompt_with_output_budget,
    should_print_console,
)
from .policy import (
    audit_round_context,
    audit_round_dir,
    consensus_failure_allows_continuation,
    resolve_audit_rounds,
    resolve_remediation_depth,
    review_loop_can_finish,
    split_sequential_review_loop_providers,
)
from .rounds import execute_single_audit_round, run_executor_round
from .state import build_review_loop_status_payload, persist_review_loop_status
from .types import (
    AuditRoundRequest,
    AuditRoundResult,
    ExecutorRoundArtifact,
    ExecutorRoundRequest,
    ReviewLoopProgress,
    ReviewLoopRunContext,
)

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
# 14. Malformed reviewer output: normalized as non-approval, not a runtime failure.
# 15. Reviewer mutation of node-local artifacts or review-state: warning-level drift.
# 16. Reviewer mutation of reserved run-root artifacts: fatal when attributable.
# 17. Consensus exhaustion with a valid candidate: persist status and apply continuation policy.
# 18. No valid canonical candidate across all audits: hard failure.
# In attributable windows, the event log may only gain records emitted by this
# runtime invocation; concurrent node windows only reject destructive event-log drift.


def _emit_consensus_exhaustion(
    telemetry: ExecutionTelemetry | None,
    node_id: str,
    executed_audit_rounds: int,
    continuation_reason: str | None,
) -> None:
    message = (
        f"Sequential node '{node_id}' failed to reach consensus after "
        f"{executed_audit_rounds} audit rounds."
    )
    if continuation_reason is not None:
        message = f"{message} Continuing due to {continuation_reason}."
    emit_runtime_log(
        telemetry,
        level="warning",
        message=message,
        operation="review_loop_consensus_exhausted",
        context=RuntimeEventContext(node_id=node_id),
        attributes={"continued": continuation_reason is not None},
    )


def _emit_no_canonical_candidate(
    telemetry: ExecutionTelemetry | None,
    node_id: str,
) -> None:
    emit_runtime_log(
        telemetry,
        level="error",
        message=(
            f"Sequential node '{node_id}' did not produce any valid canonical "
            "candidate across all audit rounds."
        ),
        operation="review_loop_no_canonical_candidate",
        context=RuntimeEventContext(node_id=node_id),
    )


async def execute_review_loop_stage(
    stage: PreflightExecutionNode,
    output: ArtifactStorePort,
    node_dir: Path,
    runtime_context: CompiledRuntimeContext,
    invoker: AgentInvoker,
    telemetry: ExecutionTelemetry | None,
) -> None:
    """Run the full sequential review loop, including exhaustion handling."""
    executors, reviewers = split_sequential_review_loop_providers(
        stage.id,
        stage.provider_records,
    )
    executor_prompt = resolve_prompt_with_output_budget(
        runtime_context,
        stage,
        output,
        role="executor",
        telemetry=telemetry,
    )
    reviewer_prompt_context = resolve_prompt_with_output_budget(
        runtime_context,
        stage,
        output,
        role="reviewer",
        telemetry=telemetry,
    )
    context = ReviewLoopRunContext(
        runtime_context=runtime_context,
        stage=stage,
        output=output,
        node_dir=node_dir,
        invoker=invoker,
        telemetry=telemetry,
        executors=tuple(executors),
        reviewers=tuple(reviewers),
        executor_prompt=executor_prompt,
        reviewer_prompt_context=reviewer_prompt_context,
        remediation_depth=resolve_remediation_depth(stage),
        audit_rounds=resolve_audit_rounds(stage),
    )
    progress = ReviewLoopProgress()

    for audit_round_num in range(1, context.audit_rounds + 1):
        progress.executed_audit_rounds = audit_round_num
        audit_result = await _execute_review_loop_audit_round(
            context,
            progress,
            audit_round_num,
        )
        if review_loop_can_finish(
            context,
            audit_result,
            audit_round_num,
        ):
            return

    if progress.latest_executor_outputs is None:
        progress.mark_consensus_exhausted(continued=False)
        _persist_review_loop_status(context, progress)
        _emit_no_canonical_candidate(context.telemetry, context.stage.id)
        raise RuntimeError(
            f"Sequential node '{context.stage.id}' did not produce a valid canonical candidate."
        )

    should_continue, continuation_reason = consensus_failure_allows_continuation(
        context.stage,
        context.runtime_context.sequential_consensus_on_exhaustion(),
    )
    if not should_continue:
        progress.mark_consensus_exhausted(continued=False)
        _persist_review_loop_status(context, progress)
        _emit_consensus_exhaustion(
            telemetry=context.telemetry,
            node_id=context.stage.id,
            executed_audit_rounds=progress.executed_audit_rounds,
            continuation_reason=None,
        )
        raise RuntimeError(
            f"Sequential node '{context.stage.id}' failed to reach consensus after "
            f"{progress.executed_audit_rounds} audit rounds."
        )

    progress.mark_consensus_exhausted(continued=True)
    _persist_review_loop_status(context, progress)
    _emit_consensus_exhaustion(
        telemetry=context.telemetry,
        node_id=context.stage.id,
        executed_audit_rounds=progress.executed_audit_rounds,
        continuation_reason=continuation_reason,
    )
    if should_print_console(context.telemetry):
        execution_console(context.telemetry).print(
            "[yellow]WARN[/] "
            f"Sequential node '{context.stage.id}' failed to reach consensus after "
            f"{progress.executed_audit_rounds} audit rounds. Continuing due to "
            f"{continuation_reason}."
        )


async def _execute_review_loop_audit_round(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
    audit_round_num: int,
) -> AuditRoundResult:
    audit_dir = audit_round_dir(
        context.node_dir,
        context.audit_rounds,
        audit_round_num,
    )
    audit_context = audit_round_context(
        context.audit_rounds,
        audit_round_num,
    )
    _print_audit_round_header(context, audit_round_num)
    initial_executor_outputs = await _initial_audit_executor_outputs(
        context,
        progress,
        audit_dir,
        audit_context,
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
            reviewer_prompt_context=context.reviewer_prompt_context,
            audit_dir=audit_dir,
            remediation_depth=context.remediation_depth,
            initial_executor_outputs=initial_executor_outputs,
            audit_round_num=audit_context,
        )
    )
    progress.record_audit_result(audit_result)
    _persist_review_loop_status(context, progress)
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


async def _initial_audit_executor_outputs(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
    audit_dir: Path,
    audit_context: int | None,
) -> list[ExecutorRoundArtifact]:
    if progress.latest_executor_outputs is not None:
        return _seed_executor_outputs(
            artifact_dir=audit_dir,
            executor_outputs=progress.latest_executor_outputs,
            round_num=1,
        )

    executor_run = await run_executor_round(
        ExecutorRoundRequest(
            runtime_context=context.runtime_context,
            node=context.stage,
            output=context.output,
            node_dir=context.node_dir,
            invoker=context.invoker,
            telemetry=context.telemetry,
            executors=context.executors,
            audit_round_num=audit_context,
            round_num=1,
            artifact_dir=audit_dir,
            executor_prompt=context.executor_prompt,
            previous_review_packet=None,
            previous_executor_outputs=None,
        )
    )
    progress.record_initial_executor_run(executor_run)
    return executor_run.outputs


def _seed_executor_outputs(
    artifact_dir: Path,
    executor_outputs: list[ExecutorRoundArtifact],
    round_num: int,
) -> list[ExecutorRoundArtifact]:
    seeded_outputs: list[ExecutorRoundArtifact] = []
    for artifact in executor_outputs:
        output_file = artifact_dir / f"{artifact.task_id}_round{round_num}.md"
        output_file.write_text(artifact.content, encoding="utf-8")
        seeded_outputs.append(
            ExecutorRoundArtifact(
                provider=artifact.provider,
                task_id=artifact.task_id,
                content=artifact.content,
                output_file=output_file,
            )
        )
    return seeded_outputs


def _persist_review_loop_status(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
) -> Path:
    payload = build_review_loop_status_payload(
        node_id=context.stage.id,
        node_dir=context.node_dir,
        progress=progress,
    )
    return persist_review_loop_status(context.node_dir, payload)
