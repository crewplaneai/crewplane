from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator_cli.architecture.contracts import AgentInvoker
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.artifacts.failure_artifacts import (
    build_invocation_failure_artifact,
)
from orchestrator_cli.core.preflight.models import PreflightExecutionNode
from orchestrator_cli.runtime.agent.failures import InvocationFailureError

from .common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    ParallelInvocation,
    ParallelResultSummary,
    ProviderCallDisplay,
    ProviderCallRequest,
    execution_console,
    resolve_prompt_with_output_budget_details,
    run_provider_invocation,
    should_print_console,
)
from .workspace_files import ResolvedWorkspaceFile

type ParallelInvocationResult = Path | Exception


def _build_parallel_invocations(
    runtime_context: CompiledRuntimeContext,
    node: PreflightExecutionNode,
    node_dir: Path,
    base_prompt: str,
    workspace_files: tuple[ResolvedWorkspaceFile, ...],
    telemetry: ExecutionTelemetry | None,
) -> list[ParallelInvocation]:
    invocations: list[ParallelInvocation] = []
    for provider in node.provider_records:
        if provider.role != "executor":
            raise ValueError(
                f"Parallel node '{node.id}' does not allow reviewer roles."
            )
        runtime_context.agent_config_for_provider(provider)
        output_file = node_dir / f"{provider.task_id}_round1.md"
        if should_print_console(telemetry):
            execution_console(telemetry).print(
                f"[dim]→ starting {provider.provider}[/]"
            )
        invocations.append(
            ParallelInvocation(
                provider=provider,
                prompt=base_prompt,
                output_file=output_file,
                task_id=provider.task_id,
                workspace_files=workspace_files,
            )
        )
    return invocations


async def _run_parallel_invocations(
    runtime_context: CompiledRuntimeContext,
    node: PreflightExecutionNode,
    output: ArtifactStorePort,
    invocations: list[ParallelInvocation],
    invoker: AgentInvoker,
    telemetry: ExecutionTelemetry | None,
) -> list[ParallelInvocationResult]:
    invocation_semaphore: asyncio.Semaphore | None = None
    max_parallel_invocations = runtime_context.max_parallel_invocations()
    if max_parallel_invocations is not None:
        invocation_semaphore = asyncio.Semaphore(max_parallel_invocations)

    async def _run_single(
        index: int, invocation: ParallelInvocation
    ) -> tuple[int, ParallelInvocationResult]:
        request = ProviderCallRequest(
            runtime_context=runtime_context,
            output=output,
            node_id=node.id,
            provider=invocation.provider,
            task_id=invocation.task_id,
            audit_round_num=None,
            round_num=1,
            prompt=invocation.prompt,
            output_file=invocation.output_file,
            role_label="executor",
            invoker=invoker,
            telemetry=telemetry,
            findings_enabled=node.findings,
            rendered_workspace_files=invocation.workspace_files,
        )
        result = await run_provider_invocation(
            request,
            invocation_semaphore=invocation_semaphore,
            capture_exception=True,
            display=ProviderCallDisplay(
                telemetry=telemetry,
                show_console_summary=False,
            ),
        )
        if result.error is not None:
            return index, result.error
        return index, result.output_file

    tasks = [
        asyncio.create_task(_run_single(index, invocation))
        for index, invocation in enumerate(invocations)
    ]
    completed = await asyncio.gather(*tasks)
    results_by_index = dict(completed)
    return [results_by_index[index] for index in range(len(invocations))]


def _write_parallel_failure_artifact(
    invocation: ParallelInvocation,
    error: Exception,
) -> None:
    invocation.output_file.write_text(
        build_invocation_failure_artifact(
            provider=invocation.provider.provider,
            task_id=invocation.task_id,
            error=str(error),
            failure_kind=(
                error.kind if isinstance(error, InvocationFailureError) else None
            ),
            failure_advice=(
                error.advice if isinstance(error, InvocationFailureError) else None
            ),
        ),
        encoding="utf-8",
    )


def _record_parallel_failure(
    invocation: ParallelInvocation,
    result: ParallelInvocationResult,
    telemetry: ExecutionTelemetry | None,
) -> int:
    if not isinstance(result, Exception):
        if should_print_console(telemetry):
            execution_console(telemetry).print(
                f"[green]✓[/] {invocation.task_id} → {invocation.output_file.name}"
            )
        return 0
    if should_print_console(telemetry):
        execution_console(telemetry).print(
            f"[red]✗[/] {invocation.task_id} failed: {result}"
        )
    _write_parallel_failure_artifact(invocation, result)
    return 1


def enforce_parallel_failure_policy(
    node: PreflightExecutionNode,
    summary: ParallelResultSummary,
    telemetry: ExecutionTelemetry | None,
) -> bool:
    if summary.failed and _lineage_worktree_node(node):
        raise RuntimeError(
            f"Parallel node '{node.id}' uses a lineage-producing worktree and "
            "cannot continue after executor failure."
        )
    failure_threshold = node.execution_policy.failure_threshold
    allowed_failures = failure_threshold if failure_threshold is not None else 0
    continue_on_failure = node.execution_policy.continue_on_failure
    if summary.failed <= allowed_failures:
        return True
    message = (
        f"Parallel node '{node.id}' exceeded failure threshold: "
        f"{summary.failed}/{summary.total} failed (allowed {allowed_failures})."
    )
    if not continue_on_failure:
        raise RuntimeError(message)
    if should_print_console(telemetry):
        execution_console(telemetry).print(
            f"[yellow]WARN[/] {message} Continuing due to continue_on_failure=true."
        )
    return False


def _lineage_worktree_node(node: PreflightExecutionNode) -> bool:
    policy = node.workspace_policy
    return (
        policy is not None
        and policy.enabled
        and policy.materialization == "worktree_checkout"
        and policy.lineage_producer
    )


def _warn_on_partial_parallel_failures(
    node: PreflightExecutionNode,
    summary: ParallelResultSummary,
    telemetry: ExecutionTelemetry | None,
) -> None:
    if not summary.failed:
        return
    if should_print_console(telemetry):
        execution_console(telemetry).print(
            "[yellow]WARN[/] Parallel node "
            f"'{node.id}' completed with partial failures: "
            f"{summary.successful}/{summary.total} succeeded."
        )


def _handle_parallel_results(
    node: PreflightExecutionNode,
    results: list[ParallelInvocationResult],
    invocations: list[ParallelInvocation],
    telemetry: ExecutionTelemetry | None,
) -> ParallelResultSummary:
    failed_count = sum(
        _record_parallel_failure(invocation, result, telemetry)
        for invocation, result in zip(invocations, results, strict=True)
    )
    summary = ParallelResultSummary(
        total=len(invocations),
        successful=len(invocations) - failed_count,
        failed=failed_count,
    )
    should_warn_partial = enforce_parallel_failure_policy(node, summary, telemetry)
    if should_warn_partial:
        _warn_on_partial_parallel_failures(node, summary, telemetry)
    return summary


async def execute_parallel_stage(
    stage: PreflightExecutionNode,
    output: ArtifactStorePort,
    runtime_context: CompiledRuntimeContext,
    invoker: AgentInvoker,
    telemetry: ExecutionTelemetry | None = None,
) -> None:
    """Execute all providers in parallel with a shared node prompt."""
    node_dir = output.create_stage_dir(stage.id)
    resolved_prompt = resolve_prompt_with_output_budget_details(
        runtime_context,
        stage,
        output,
        role="executor",
        telemetry=telemetry,
    )
    invocations = _build_parallel_invocations(
        runtime_context,
        stage,
        node_dir,
        resolved_prompt.text,
        resolved_prompt.workspace_files,
        telemetry=telemetry,
    )
    results = await _run_parallel_invocations(
        runtime_context,
        stage,
        output,
        invocations,
        invoker=invoker,
        telemetry=telemetry,
    )
    _handle_parallel_results(stage, results, invocations, telemetry)
