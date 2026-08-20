from __future__ import annotations

import asyncio
import hashlib
import tempfile
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

from crewplane.architecture.safe_files import (
    contained_directory,
    contained_regular_file,
)
from crewplane.artifacts.atomic import atomic_write_text
from crewplane.core.preflight.models import ProviderRecord
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.failures import InvocationFailureError
from crewplane.runtime.workspace.setup import WorkspaceSetupError

from ..common import (
    ExecutionTelemetry,
    ProviderCallDisplay,
    execution_console,
    should_print_console,
)
from ..consensus import evaluate_review_output
from ..errors import NodeExecutionError, is_expected_execution_failure
from ..provider_call import (
    bind_invocation_output,
    publish_invocation_output,
    read_bound_invocation_output,
)
from .drift import (
    create_drift_guard_session,
    run_provider_call_with_drift_guard,
)
from .drift.capture import capture_drift_recovery_baseline
from .prompts import REVIEWER_ONLY_INSTRUCTION, build_reviewer_prompt
from .state import (
    persist_review_evaluation_artifacts,
    persist_review_state,
    persist_reviewer_failure_state,
)
from .types import (
    DriftGuardCallRequest,
    DriftGuardSession,
    GeneratedFileDriftAllowance,
    ReviewerInvocationFailure,
    ReviewerInvocationResult,
    ReviewerRoundArtifact,
    ReviewerRoundRequest,
    ReviewerRoundRunResult,
    ReviewerRoundRuntime,
)
from .validation import emit_review_evaluation_warnings, emit_reviewer_failure_warning
from .workspace_state_paths import workspace_artifact_allowed_paths


class ReviewerOutputMissingError(NodeExecutionError):
    """Raised when a reviewer invocation produced no review text."""


class ReviewerOutputPublicationError(NodeExecutionError):
    """Raised when bound reviewer bytes cannot be published safely."""


async def run_reviewer_round(
    request: ReviewerRoundRequest,
) -> ReviewerRoundRunResult:
    runtime = build_reviewer_round_runtime(request)
    if len(request.reviewers) > 1 and should_print_console(request.telemetry):
        execution_console(request.telemetry).print(
            f"Running {len(request.reviewers)} reviewers in parallel "
            f"for round {request.round_num}..."
        )

    with ExitStack() as private_outputs:
        invocation_output_files = [
            Path(
                private_outputs.enter_context(
                    tempfile.TemporaryDirectory(prefix="crewplane-reviewer-")
                )
            )
            / "provider-output.md"
            for _provider in request.reviewers
        ]
        tasks = [
            asyncio.create_task(
                invoke_reviewer_with_drift_guard(
                    request,
                    runtime,
                    index,
                    provider,
                    invocation_output_files[index],
                )
            )
            for index, provider in enumerate(request.reviewers)
        ]
        try:
            completed = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
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

    protected_output_paths = {
        reviewer_output_path(request, provider)[1] for provider in request.reviewers
    }
    runtime_owned_paths = {
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
    }
    if request.output.log_cli_output:
        log_path = request.node.artifact_contract.log_path
        if log_path is None:
            raise ValueError("Reviewer log capture requires a compiled log locator.")
        runtime_owned_roots = {request.output.stages_dir / log_path}
    else:
        runtime_owned_roots = set()
    if protected_output_paths.intersection(runtime_owned_paths):
        raise RuntimeError("Reviewer output and runtime-owned paths must be distinct.")
    quiet_telemetry = quiet_telemetry_for_reviewer_round(request.telemetry)
    drift_session = DriftGuardSession(
        telemetry=quiet_telemetry,
        event_log_capture=None,
        generated_file_allowance=GeneratedFileDriftAllowance(),
        runtime_publications=request.runtime_context.runtime_publications,
        recovery_baseline=capture_drift_recovery_baseline(
            request.node_dir,
            request.output,
            request.runtime_context.runtime_publications,
            runtime_owned_paths,
            runtime_owned_roots,
        ),
    )
    return ReviewerRoundRuntime(
        reviewer_prompt=reviewer_prompt,
        invocation_semaphore=invocation_semaphore,
        drift_session=drift_session,
        protected_output_paths=protected_output_paths,
        runtime_owned_paths=runtime_owned_paths,
        runtime_owned_roots=runtime_owned_roots,
    )


async def invoke_reviewer_with_drift_guard(
    request: ReviewerRoundRequest,
    runtime: ReviewerRoundRuntime,
    index: int,
    provider: ProviderRecord,
    invocation_output_file: Path,
) -> ReviewerInvocationResult:
    task_id, output_file = reviewer_output_path(request, provider)
    allowed_paths = workspace_artifact_allowed_paths(
        request.output,
        request.node,
        provider.task_id,
        ProviderRole.REVIEWER,
        request.audit_round_num,
        request.round_num,
    )

    async def invoke_reviewer() -> int:
        invocation_session = replace(
            create_drift_guard_session(
                runtime.drift_session.telemetry,
                runtime.drift_session.runtime_publications,
                runtime.drift_session.generated_file_allowance,
            ),
            recovery_baseline=runtime.drift_session.recovery_baseline,
        )
        return await run_provider_call_with_drift_guard(
            DriftGuardCallRequest(
                runtime_context=request.runtime_context,
                output=request.output,
                node=request.node,
                node_dir=request.node_dir,
                invoker=request.invoker,
                telemetry=invocation_session.telemetry,
                audit_round_num=request.audit_round_num,
                round_num=request.round_num,
                provider=provider,
                task_id=task_id,
                prompt=runtime.reviewer_prompt,
                output_file=output_file,
                role_label=ProviderRole.REVIEWER,
                findings_enabled=False,
                allowed_paths=allowed_paths,
                display=ProviderCallDisplay(
                    telemetry=invocation_session.telemetry,
                    progress_description=f"Reviewing with {provider.provider}...",
                ),
                drift_session=invocation_session,
                rendered_workspace_files=request.reviewer_prompt_workspace_files,
                invocation_output_file=invocation_output_file,
                defer_output_publication=True,
                protected_paths=runtime.protected_output_paths,
                runtime_owned_paths=runtime.runtime_owned_paths,
                runtime_owned_roots=runtime.runtime_owned_roots,
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
        invocation_output_file=invocation_output_file,
        output_signature=bind_invocation_output(invocation_output_file),
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
        if isinstance(result, BaseException):
            if not is_expected_execution_failure(result):
                raise result
            assert isinstance(result, Exception)
            invocation_failures.append(
                ReviewerInvocationFailure(
                    index=index,
                    provider=provider,
                    task_id=task_id,
                    output_file=output_file,
                    error=result,
                    failure_kind=reviewer_failure_kind(result),
                    warning=reviewer_failure_warning(result),
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


def reviewer_failure_kind(error: Exception) -> str:
    if isinstance(error, InvocationFailureError):
        return "invocation_failed"
    if isinstance(error, WorkspaceSetupError):
        return "workspace_setup_failed"
    return "node_execution_failed"


def reviewer_failure_warning(error: Exception) -> str:
    if isinstance(error, InvocationFailureError):
        return (
            "Reviewer invocation failed. Preserving failure state "
            "without treating provider metadata as review feedback."
        )
    return (
        "Reviewer execution failed. Preserving failure state "
        "without treating the failure as review feedback."
    )


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
        except ReviewerOutputPublicationError as exc:
            failures.append(
                ReviewerInvocationFailure(
                    index=result.index,
                    provider=result.provider,
                    task_id=result.task_id,
                    output_file=result.output_file,
                    error=exc,
                    failure_kind="output_publication_failed",
                    warning=(
                        "Reviewer output could not be published safely. Preserving "
                        "failure state without treating it as review feedback."
                    ),
                )
            )
    return outputs, failures


def evaluate_reviewer_output(
    request: ReviewerRoundRequest,
    invocation_result: ReviewerInvocationResult,
) -> ReviewerRoundArtifact:
    raw_output = read_reviewer_output(invocation_result)
    evaluation = evaluate_review_output(raw_output)
    normalized_payload = evaluation.normalized_markdown.encode("utf-8")
    normalized_signature = (
        len(normalized_payload),
        hashlib.sha256(normalized_payload).hexdigest(),
    )
    with tempfile.TemporaryDirectory(prefix="crewplane-review-normalized-") as temp_dir:
        normalized_output = Path(temp_dir) / "provider-output.md"
        atomic_write_text(normalized_output, evaluation.normalized_markdown)
        with request.runtime_context.runtime_publications.transaction():
            try:
                published_signature = publish_invocation_output(
                    normalized_output,
                    invocation_result.output_file,
                    request.runtime_context.runtime_publications,
                    normalized_signature,
                )
            except (OSError, RuntimeError) as exc:
                raise ReviewerOutputPublicationError(
                    "Reviewer output did not match its bound runtime publication."
                ) from exc
            persist_review_evaluation_artifacts(
                invocation_result.output_file,
                evaluation,
            )
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
        audit_round_num=request.audit_round_num,
        round_num=request.round_num,
        output_signature=published_signature,
    )
    persist_review_state(
        artifact_dir=request.artifact_dir,
        audit_round_num=request.audit_round_num,
        round_num=request.round_num,
        reviewer_output=reviewer_output,
    )
    return reviewer_output


def read_reviewer_output(invocation_result: ReviewerInvocationResult) -> str:
    try:
        raw_output = read_bound_invocation_output(
            invocation_result.invocation_output_file,
            invocation_result.output_signature,
        )
    except RuntimeError as exc:
        raise ReviewerOutputPublicationError(
            "Reviewer output did not match its bound provider bytes."
        ) from exc
    if not raw_output.strip():
        raise ReviewerOutputMissingError("No review content was extracted.")
    return raw_output


def _safe_review_output_path(
    output_file: Path,
    node_dir: Path,
    missing_ok: bool = False,
) -> Path | None:
    try:
        relative_path = output_file.relative_to(node_dir).as_posix()
    except ValueError as exc:
        raise RuntimeError(
            f"Reviewer output is outside its node stage: {output_file.as_posix()}"
        ) from exc
    _safe_output_parent(node_dir, relative_path, output_file)
    safe_output = contained_regular_file(node_dir, relative_path)
    if safe_output is not None:
        return safe_output
    if missing_ok:
        try:
            output_file.lstat()
        except FileNotFoundError:
            return None
    raise RuntimeError(
        f"Reviewer output is missing or unsafe: {output_file.as_posix()}"
    )


def _safe_output_parent(
    node_dir: Path,
    relative_path: str,
    output_file: Path,
) -> None:
    parent_relative_path = Path(relative_path).parent.as_posix()
    if parent_relative_path == ".":
        parent_relative_path = ""
    try:
        safe_parent = contained_directory(node_dir, parent_relative_path)
    except ValueError as exc:
        raise RuntimeError(
            f"Reviewer output parent is unsafe: {output_file.as_posix()}"
        ) from exc
    if safe_parent is None:
        raise RuntimeError(
            f"Reviewer output parent is missing or unsafe: {output_file.as_posix()}"
        )


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
    raise NodeExecutionError(
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
