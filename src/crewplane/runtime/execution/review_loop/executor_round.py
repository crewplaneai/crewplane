from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts.artifacts import build_task_round_filename
from crewplane.architecture.safe_files import (
    contained_directory,
    contained_regular_file,
)
from crewplane.core.file_hashing import ContentSignature
from crewplane.core.preflight.models import ProviderRecord
from crewplane.core.workflow.keywords import ProviderRole

from ..common import ProviderCallDisplay, resolve_prompt_with_output_budget_details
from ..fragment_assembler import ResolvedPrompt
from ..provider_call import ProviderOutputPolicy, read_bound_invocation_output
from ..workspace_files.source_resolution import WorkspaceCandidateSourceContext
from .candidate_identity import bind_candidate_identities, observe_project
from .drift import run_provider_call_with_drift_guard
from .prompts import (
    build_executor_prompt,
    resolve_previous_candidate_context,
)
from .types import (
    DriftGuardCallRequest,
    ExecutorRoundArtifact,
    ExecutorRoundRequest,
    ExecutorRoundRunResult,
)
from .workspace_state_paths import workspace_artifact_allowed_paths


async def run_executor_round(
    request: ExecutorRoundRequest,
) -> ExecutorRoundRunResult:
    """Run executors in order and bind identities to the completed round."""
    prompt = _prepare_executor_prompt(request)
    project_before = await observe_project(request)
    executor_outputs: list[ExecutorRoundArtifact] = []
    drift_warning_count = 0
    for provider in request.executors:
        call = _build_executor_call(request, provider, prompt)
        artifact, warning_count = await _run_executor(call)
        executor_outputs.append(artifact)
        drift_warning_count += warning_count
    return ExecutorRoundRunResult(
        outputs=await bind_candidate_identities(
            request, executor_outputs, project_before
        ),
        drift_warning_count=drift_warning_count,
    )


def _prepare_executor_prompt(request: ExecutorRoundRequest) -> ResolvedPrompt:
    previous_candidate_context = resolve_previous_candidate_context(
        request.node,
        request.previous_executor_outputs,
        request.telemetry,
    )
    resolved_prompt = ResolvedPrompt(
        request.executor_prompt, request.executor_prompt_workspace_files
    )
    if request.previous_executor_outputs is not None:
        resolved_prompt = resolve_prompt_with_output_budget_details(
            request.runtime_context,
            request.node,
            request.output,
            role=ProviderRole.EXECUTOR,
            telemetry=request.telemetry,
            workspace_candidate_source=True,
            workspace_candidate_context=WorkspaceCandidateSourceContext(
                role_label=ProviderRole.EXECUTOR,
                round_num=request.round_num,
                audit_round_num=request.audit_round_num,
            ),
        )
    executor_prompt = build_executor_prompt(
        resolved_prompt.text,
        previous_candidate_context,
        request.previous_review_packet,
        request.initial_review_handoff,
        request.recovery_attempt,
    )
    return ResolvedPrompt(executor_prompt, resolved_prompt.workspace_files)


def _build_executor_call(
    request: ExecutorRoundRequest,
    provider: ProviderRecord,
    prompt: ResolvedPrompt,
) -> DriftGuardCallRequest:
    output_file = request.artifact_dir / build_task_round_filename(
        provider.task_id, request.round_num
    )
    allowed_paths = {output_file}
    allowed_paths.update(
        workspace_artifact_allowed_paths(
            request.output,
            request.node,
            provider.task_id,
            ProviderRole.EXECUTOR,
            request.audit_round_num,
            request.round_num,
        ),
    )
    return DriftGuardCallRequest(
        runtime_context=request.runtime_context,
        output=request.output,
        node=request.node,
        node_dir=request.node_dir,
        invoker=request.invoker,
        telemetry=request.telemetry,
        audit_round_num=request.audit_round_num,
        round_num=request.round_num,
        provider=provider,
        task_id=provider.task_id,
        prompt=prompt.text,
        output_file=output_file,
        role_label=ProviderRole.EXECUTOR,
        findings_enabled=request.node.findings,
        allowed_paths=allowed_paths,
        display=ProviderCallDisplay(
            telemetry=request.telemetry,
            progress_description=f"Executing {provider.provider}...",
        ),
        drift_session=None,
        provider_output_policy=executor_output_policy(request),
        rendered_workspace_files=prompt.workspace_files,
    )


async def _run_executor(
    call: DriftGuardCallRequest,
) -> tuple[ExecutorRoundArtifact, int]:
    warning_count = await run_provider_call_with_drift_guard(call)
    published_signatures = call.runtime_context.runtime_publications.snapshot()[0]
    content, output_signature = _read_executor_output(
        call.output_file, call.node_dir, published_signatures.get(call.output_file)
    )
    artifact = ExecutorRoundArtifact(
        provider=call.provider,
        task_id=call.task_id,
        content=content,
        output_file=call.output_file,
        audit_round_num=call.audit_round_num,
        round_num=call.round_num,
        output_signature=output_signature,
    )
    return artifact, warning_count


def executor_output_policy(
    request: ExecutorRoundRequest,
) -> ProviderOutputPolicy:
    if request.round_num > 1:
        return ProviderOutputPolicy.ALLOW_MISSING_OUTPUT
    return ProviderOutputPolicy.REQUIRE_OUTPUT


def _read_executor_output(
    output_file: Path,
    node_dir: Path,
    expected_signature: ContentSignature | None,
) -> tuple[str, ContentSignature | None]:
    safe_output = _resolve_safe_executor_output(output_file, node_dir)
    if safe_output is None:
        return "", None
    if expected_signature is None:
        raise RuntimeError(
            "Executor output does not have a bound runtime publication: "
            f"{output_file.as_posix()}"
        )
    return (
        read_bound_invocation_output(safe_output, expected_signature),
        expected_signature,
    )


def _resolve_safe_executor_output(output_file: Path, node_dir: Path) -> Path | None:
    try:
        relative_path = output_file.relative_to(node_dir).as_posix()
    except ValueError as exc:
        raise RuntimeError(
            f"Executor output is outside its node stage: {output_file.as_posix()}"
        ) from exc
    _safe_output_parent(node_dir, relative_path, output_file)
    safe_output = contained_regular_file(node_dir, relative_path)
    if safe_output is None:
        try:
            output_file.lstat()
        except FileNotFoundError:
            return None
        raise RuntimeError(
            f"Executor output is missing or unsafe: {output_file.as_posix()}"
        )
    return safe_output


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
            f"Executor output parent is unsafe: {output_file.as_posix()}"
        ) from exc
    if safe_parent is None:
        raise RuntimeError(
            f"Executor output parent is missing or unsafe: {output_file.as_posix()}"
        )
