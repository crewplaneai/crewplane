from __future__ import annotations

from pathlib import Path

from orchestrator_cli.architecture.contracts import AgentInvoker
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight.models import PreflightExecutionNode

from .common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    ProviderCallDisplay,
    ProviderCallRequest,
    resolve_prompt_with_output_budget_details,
    run_provider_call,
)
from .review_loop import execute_review_loop_stage
from .workspace_files import WorkspaceCandidateSourceContext

DEFAULT_SINGLE_PROVIDER_ROUNDS = 1


def _resolve_single_provider_rounds(node: PreflightExecutionNode) -> int:
    depth = node.execution_policy.depth
    if depth is not None and depth <= 0:
        raise ValueError(
            f"Sequential node '{node.id}' depth must be greater than 0 when provided."
        )
    return depth or DEFAULT_SINGLE_PROVIDER_ROUNDS


async def _execute_single_provider_sequential_node(
    node: PreflightExecutionNode,
    output: ArtifactStorePort,
    node_dir: Path,
    max_rounds: int,
    runtime_context: CompiledRuntimeContext,
    invoker: AgentInvoker,
    telemetry: ExecutionTelemetry | None,
) -> None:
    provider = node.provider_records[0]
    for round_num in range(1, max_rounds + 1):
        resolved_prompt = resolve_prompt_with_output_budget_details(
            runtime_context,
            node,
            output,
            role="executor",
            telemetry=telemetry,
            workspace_candidate_source=round_num > 1,
            workspace_candidate_context=(
                WorkspaceCandidateSourceContext(
                    role_label="executor",
                    round_num=round_num,
                    audit_round_num=None,
                )
                if round_num > 1
                else None
            ),
        )
        output_file = node_dir / f"{provider.task_id}_round{round_num}.md"
        await run_provider_call(
            ProviderCallRequest(
                runtime_context=runtime_context,
                output=output,
                node_id=node.id,
                provider=provider,
                task_id=provider.task_id,
                audit_round_num=None,
                round_num=round_num,
                prompt=resolved_prompt.text,
                output_file=output_file,
                role_label="executor",
                invoker=invoker,
                telemetry=telemetry,
                findings_enabled=node.findings,
                rendered_workspace_files=resolved_prompt.workspace_files,
            ),
            display=ProviderCallDisplay(
                telemetry=telemetry,
                progress_description=f"Executing {provider.provider}...",
            ),
        )


async def execute_sequential_stage(
    stage: PreflightExecutionNode,
    output: ArtifactStorePort,
    runtime_context: CompiledRuntimeContext,
    invoker: AgentInvoker,
    telemetry: ExecutionTelemetry | None = None,
) -> None:
    """Execute a sequential stage."""
    node_dir = output.create_stage_dir(stage.id)
    if len(stage.provider_records) == 1:
        await _execute_single_provider_sequential_node(
            node=stage,
            output=output,
            node_dir=node_dir,
            max_rounds=_resolve_single_provider_rounds(stage),
            runtime_context=runtime_context,
            invoker=invoker,
            telemetry=telemetry,
        )
        return

    await execute_review_loop_stage(
        stage=stage,
        output=output,
        node_dir=node_dir,
        runtime_context=runtime_context,
        invoker=invoker,
        telemetry=telemetry,
    )
