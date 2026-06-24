from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from crewplane.core.preflight.models import ProviderRecord
from crewplane.core.workflow.keywords import ProviderRole

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
from .prompts import REVIEWER_ONLY_INSTRUCTION, build_reviewer_prompt
from .state import (
    persist_review_evaluation,
    persist_review_state,
    persist_reviewer_failure_state,
)
from .types import (
    DriftGuardCallRequest,
    ReviewerInvocationFailure,
    ReviewerInvocationResult,
    ReviewerRoundArtifact,
    ReviewerRoundRequest,
    ReviewerRoundRunResult,
    ReviewerRoundRuntime,
)
from .validation import emit_review_evaluation_warnings, emit_reviewer_failure_warning
from .workspace_state_paths import workspace_artifact_allowed_paths


class ReviewerOutputMissingError(RuntimeError):
    """Raised when a reviewer invocation produced no review text."""


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
    ordered_results, ordered_failures = collect_ordered_reviewer_results(
        request,
        completed,
    )
    ordered_outputs, output_failures = evaluate_reviewer_outputs(
        request,
        ordered_results,
    )
    ordered_failures.extend(output_failures)
    persist_reviewer_failures(request, ordered_failures)
    enforce_reviewer_failure_policy(request, ordered_failures)
    drift_warning_count = sum(result.drift_warning_count for result in ordered_results)
    return ReviewerRoundRunResult(
        outputs=ordered_outputs,
        drift_warning_count=drift_warning_count,
        reviewer_failure_count=len(ordered_failures),
    )


def build_reviewer_round_runtime(
    request: ReviewerRoundRequest,
) -> ReviewerRoundRuntime:
    reviewer_prompt = build_reviewer_prompt(
        request.reviewer_prompt_context,
        request.review_context,
        request.previous_review_packet,
        request.review_context_heading,
        request.review_context_note,
        request.reviewer_instruction or REVIEWER_ONLY_INSTRUCTION,
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
    allowed_paths.update(
        path
        for provider in request.reviewers
        for path in workspace_artifact_allowed_paths(
            request.output,
            request.node,
            provider.task_id,
            ProviderRole.REVIEWER,
            request.audit_round_num,
            request.round_num,
        )
    )
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
                role_label=ProviderRole.REVIEWER,
                findings_enabled=False,
                allowed_paths=runtime.allowed_paths,
                display=ProviderCallDisplay(
                    telemetry=runtime.drift_session.telemetry,
                    progress_description=f"Reviewing with {provider.provider}...",
                ),
                drift_session=runtime.drift_session,
                rendered_workspace_files=request.reviewer_prompt_workspace_files,
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
    request: ReviewerRoundRequest,
    completed: list[ReviewerInvocationResult | BaseException],
) -> tuple[list[ReviewerInvocationResult], list[ReviewerInvocationFailure]]:
    invocation_results: list[ReviewerInvocationResult] = []
    invocation_failures: list[ReviewerInvocationFailure] = []
    for index, result in enumerate(completed):
        provider = request.reviewers[index]
        task_id, output_file = reviewer_output_path(request, provider)
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result
        if isinstance(result, BaseException):
            invocation_failures.append(
                ReviewerInvocationFailure(
                    index=index,
                    provider=provider,
                    task_id=task_id,
                    output_file=output_file,
                    error=result,
                    failure_kind="invocation_failed",
                    warning=(
                        "Reviewer invocation failed. Preserving failure state "
                        "without treating provider metadata as review feedback."
                    ),
                )
            )
            continue
        invocation_results.append(result)

    results_by_index = {result.index: result for result in invocation_results}
    ordered_results = [
        results_by_index[index]
        for index in range(len(request.reviewers))
        if index in results_by_index
    ]
    return ordered_results, invocation_failures


def evaluate_reviewer_outputs(
    request: ReviewerRoundRequest,
    invocation_results: list[ReviewerInvocationResult],
) -> tuple[list[ReviewerRoundArtifact], list[ReviewerInvocationFailure]]:
    outputs: list[ReviewerRoundArtifact] = []
    failures: list[ReviewerInvocationFailure] = []
    for result in invocation_results:
        try:
            outputs.append(evaluate_reviewer_output(request, result))
        except ReviewerOutputMissingError as exc:
            failures.append(
                ReviewerInvocationFailure(
                    index=result.index,
                    provider=result.provider,
                    task_id=result.task_id,
                    output_file=result.output_file,
                    error=exc,
                    failure_kind="missing_review_content",
                    warning=(
                        "Reviewer invocation completed, but no review content was "
                        "extracted. Preserving failure state without sending "
                        "provider metadata to the executor."
                    ),
                )
            )
    return outputs, failures


def evaluate_reviewer_output(
    request: ReviewerRoundRequest,
    invocation_result: ReviewerInvocationResult,
) -> ReviewerRoundArtifact:
    raw_output = read_reviewer_output(invocation_result.output_file)
    evaluation = evaluate_review_output(raw_output)
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


def read_reviewer_output(output_file: Path) -> str:
    if not output_file.exists():
        raise ReviewerOutputMissingError("No reviewer output artifact was created.")
    raw_output = output_file.read_text(encoding="utf-8")
    if not raw_output.strip():
        raise ReviewerOutputMissingError("No review content was extracted.")
    return raw_output


def persist_reviewer_failures(
    request: ReviewerRoundRequest,
    failures: list[ReviewerInvocationFailure],
) -> None:
    for failure in failures:
        persist_reviewer_failure_state(
            artifact_dir=request.artifact_dir,
            audit_round_num=request.audit_round_num,
            round_num=request.round_num,
            failure=failure,
        )
        emit_reviewer_failure_warning(
            telemetry=request.telemetry,
            node_id=request.node.id,
            failure=failure,
            audit_round_num=request.audit_round_num,
            round_num=request.round_num,
        )


def enforce_reviewer_failure_policy(
    request: ReviewerRoundRequest,
    failures: list[ReviewerInvocationFailure],
) -> None:
    if not failures or request.node.execution_policy.continue_on_failure:
        return
    if len(failures) == 1:
        raise failures[0].error
    failure_details = "; ".join(
        f"{failure.task_id}: {failure.error}" for failure in failures
    )
    raise RuntimeError(
        f"Reviewer invocation failed for node '{request.node.id}': {failure_details}."
    ) from failures[0].error


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
