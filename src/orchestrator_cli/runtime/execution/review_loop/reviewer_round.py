from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from orchestrator_cli.core.preflight.models import ProviderRecord

from ..common import (
    ExecutionTelemetry,
    ProviderCallDisplay,
    execution_console,
    should_print_console,
)
from ..consensus import evaluate_review_output
from .drift import (
    create_drift_guard_session,
    run_provider_call_with_drift_guard,
)
from .prompts import build_reviewer_prompt
from .state import persist_review_evaluation, persist_review_state
from .types import (
    DriftGuardCallRequest,
    ReviewerInvocationResult,
    ReviewerRoundArtifact,
    ReviewerRoundRequest,
    ReviewerRoundRunResult,
    ReviewerRoundRuntime,
)
from .validation import emit_review_evaluation_warnings


async def run_reviewer_round(
    request: ReviewerRoundRequest,
) -> ReviewerRoundRunResult:
    runtime = build_reviewer_round_runtime(request)
    if len(request.reviewers) > 1 and should_print_console(request.telemetry):
        execution_console(request.telemetry).print(
            f"Running {len(request.reviewers)} reviewers in parallel "
            f"for round {request.round_num}..."
        )

    tasks = [
        asyncio.create_task(
            invoke_reviewer_with_drift_guard(request, runtime, index, provider)
        )
        for index, provider in enumerate(request.reviewers)
    ]
    completed = await asyncio.gather(*tasks, return_exceptions=True)
    ordered_results = collect_ordered_reviewer_results(
        completed,
        len(request.reviewers),
    )
    ordered_outputs = [
        evaluate_reviewer_output(request, result) for result in ordered_results
    ]
    drift_warning_count = sum(result.drift_warning_count for result in ordered_results)
    return ReviewerRoundRunResult(
        outputs=ordered_outputs,
        drift_warning_count=drift_warning_count,
    )


def build_reviewer_round_runtime(
    request: ReviewerRoundRequest,
) -> ReviewerRoundRuntime:
    reviewer_prompt = build_reviewer_prompt(
        request.reviewer_prompt_context,
        request.review_context,
        request.previous_review_packet,
    )
    invocation_semaphore: asyncio.Semaphore | None = None
    max_parallel_invocations = request.runtime_context.max_parallel_invocations()
    if max_parallel_invocations is not None:
        invocation_semaphore = asyncio.Semaphore(max_parallel_invocations)

    quiet_telemetry = quiet_telemetry_for_reviewer_round(request.telemetry)
    drift_session = create_drift_guard_session(quiet_telemetry)
    allowed_paths = {
        reviewer_output_path(request, provider)[1] for provider in request.reviewers
    }
    return ReviewerRoundRuntime(
        reviewer_prompt=reviewer_prompt,
        invocation_semaphore=invocation_semaphore,
        drift_session=drift_session,
        allowed_paths=allowed_paths,
    )


async def invoke_reviewer_with_drift_guard(
    request: ReviewerRoundRequest,
    runtime: ReviewerRoundRuntime,
    index: int,
    provider: ProviderRecord,
) -> ReviewerInvocationResult:
    task_id, output_file = reviewer_output_path(request, provider)

    async def invoke_reviewer() -> int:
        return await run_provider_call_with_drift_guard(
            DriftGuardCallRequest(
                runtime_context=request.runtime_context,
                output=request.output,
                node=request.node,
                node_dir=request.node_dir,
                invoker=request.invoker,
                telemetry=runtime.drift_session.telemetry,
                audit_round_num=request.audit_round_num,
                round_num=request.round_num,
                provider=provider,
                task_id=task_id,
                prompt=runtime.reviewer_prompt,
                output_file=output_file,
                role_label="reviewer",
                findings_enabled=False,
                allowed_paths=runtime.allowed_paths,
                display=ProviderCallDisplay(
                    telemetry=runtime.drift_session.telemetry,
                    progress_description=f"Reviewing with {provider.provider}...",
                ),
                drift_session=runtime.drift_session,
            )
        )

    if runtime.invocation_semaphore is None:
        drift_warning_count = await invoke_reviewer()
    else:
        async with runtime.invocation_semaphore:
            drift_warning_count = await invoke_reviewer()

    return ReviewerInvocationResult(
        index=index,
        provider=provider,
        task_id=task_id,
        output_file=output_file,
        drift_warning_count=drift_warning_count,
    )


def collect_ordered_reviewer_results(
    completed: list[ReviewerInvocationResult | BaseException],
    reviewer_count: int,
) -> list[ReviewerInvocationResult]:
    invocation_results: list[ReviewerInvocationResult] = []
    for result in completed:
        if isinstance(result, BaseException):
            raise result
        invocation_results.append(result)

    results_by_index = {result.index: result for result in invocation_results}
    return [results_by_index[index] for index in range(reviewer_count)]


def evaluate_reviewer_output(
    request: ReviewerRoundRequest,
    invocation_result: ReviewerInvocationResult,
) -> ReviewerRoundArtifact:
    evaluation = evaluate_review_output(
        invocation_result.output_file.read_text(encoding="utf-8")
    )
    persist_review_evaluation(invocation_result.output_file, evaluation)
    emit_review_evaluation_warnings(
        telemetry=request.telemetry,
        node_id=request.node.id,
        provider=invocation_result.provider,
        task_id=invocation_result.task_id,
        audit_round_num=request.audit_round_num,
        round_num=request.round_num,
        output_file=invocation_result.output_file,
        evaluation=evaluation,
    )
    reviewer_output = ReviewerRoundArtifact(
        provider=invocation_result.provider,
        task_id=invocation_result.task_id,
        evaluation=evaluation,
        output_file=invocation_result.output_file,
    )
    persist_review_state(
        artifact_dir=request.artifact_dir,
        audit_round_num=request.audit_round_num,
        round_num=request.round_num,
        reviewer_output=reviewer_output,
    )
    return reviewer_output


def quiet_telemetry_for_reviewer_round(
    telemetry: ExecutionTelemetry | None,
) -> ExecutionTelemetry:
    if telemetry is None:
        return ExecutionTelemetry(
            workflow_name="",
            run_id="",
            suppress_console_output=True,
        )
    return replace(telemetry, suppress_console_output=True)


def reviewer_output_path(
    request: ReviewerRoundRequest,
    provider: ProviderRecord,
) -> tuple[str, Path]:
    task_id = provider.task_id
    output_file = request.artifact_dir / f"{task_id}_round{request.round_num}.md"
    return task_id, output_file
