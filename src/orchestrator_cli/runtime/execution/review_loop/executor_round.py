from __future__ import annotations

from ..common import ProviderCallDisplay
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
    executor_prompt = build_executor_prompt(
        request.executor_prompt,
        previous_candidate_context,
        request.previous_review_packet,
    )
    for provider in request.executors:
        task_id = provider.task_id
        output_file = request.artifact_dir / f"{task_id}_round{request.round_num}.md"
        allowed_paths = {output_file}
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
                role_label="executor",
                findings_enabled=request.node.findings,
                allowed_paths=allowed_paths,
                display=ProviderCallDisplay(
                    telemetry=request.telemetry,
                    progress_description=f"Executing {provider.provider}...",
                ),
                drift_session=None,
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
