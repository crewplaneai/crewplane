from __future__ import annotations

from crewplane.core.workflow.keywords import ProviderRole

from ..common import ProviderCallDisplay, resolve_prompt_with_output_budget_details
from ..provider_call import ProviderOutputPolicy
from ..workspace_files import WorkspaceCandidateSourceContext
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
    executor_outputs: list[ExecutorRoundArtifact] = []
    drift_warning_count = 0
    previous_candidate_context = resolve_previous_candidate_context(
        request.node,
        request.previous_executor_outputs,
        request.telemetry,
    )
    base_executor_prompt = request.executor_prompt
    rendered_workspace_files = request.executor_prompt_workspace_files
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
        base_executor_prompt = resolved_prompt.text
        rendered_workspace_files = resolved_prompt.workspace_files
    executor_prompt = build_executor_prompt(
        base_executor_prompt,
        previous_candidate_context,
        request.previous_review_packet,
        request.initial_review_handoff,
    )
    for provider in request.executors:
        task_id = provider.task_id
        output_file = request.artifact_dir / f"{task_id}_round{request.round_num}.md"
        allowed_paths = {output_file}
        allowed_paths.update(
            workspace_artifact_allowed_paths(
                request.output,
                request.node,
                task_id,
                ProviderRole.EXECUTOR,
                request.audit_round_num,
                request.round_num,
            )
        )
        drift_warning_count += await run_provider_call_with_drift_guard(
            DriftGuardCallRequest(
                runtime_context=request.runtime_context,
                output=request.output,
                node=request.node,
                node_dir=request.node_dir,
                invoker=request.invoker,
                telemetry=request.telemetry,
                audit_round_num=request.audit_round_num,
                round_num=request.round_num,
                provider=provider,
                task_id=task_id,
                prompt=executor_prompt,
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
                rendered_workspace_files=rendered_workspace_files,
            )
        )
        content = (
            output_file.read_text(encoding="utf-8") if output_file.is_file() else ""
        )
        executor_outputs.append(
            ExecutorRoundArtifact(
                provider=provider,
                task_id=task_id,
                content=content,
                output_file=output_file,
            )
        )
    return ExecutorRoundRunResult(
        outputs=executor_outputs,
        drift_warning_count=drift_warning_count,
    )


def executor_output_policy(
    request: ExecutorRoundRequest,
) -> ProviderOutputPolicy:
    if request.round_num > 1:
        return ProviderOutputPolicy.ALLOW_MISSING_OUTPUT
    return ProviderOutputPolicy.REQUIRE_OUTPUT
