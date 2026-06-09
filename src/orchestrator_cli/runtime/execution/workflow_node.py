from __future__ import annotations

from orchestrator_cli.architecture.contracts import AgentInvoker
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight.models import PreflightExecutionNode

from .common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    build_stage_task_specs,
    emit_stage_finalize_logs,
    emit_workflow_event,
    execution_console,
    should_print_console,
)
from .input import execute_input_stage
from .parallel import execute_parallel_stage
from .sequential import execute_sequential_stage


def mark_node_running_activity(
    telemetry: ExecutionTelemetry | None,
    node_id: str,
) -> None:
    if telemetry is None or telemetry.activity_tracker is None:
        return
    telemetry.activity_tracker.mark_node_running(node_id)


def mark_node_finished_activity(
    telemetry: ExecutionTelemetry | None,
    node_id: str,
) -> None:
    if telemetry is None or telemetry.activity_tracker is None:
        return
    telemetry.activity_tracker.mark_node_finished(node_id)


async def execute_node(
    node: PreflightExecutionNode,
    output: ArtifactStorePort,
    invoker: AgentInvoker,
    runtime_context: CompiledRuntimeContext,
    telemetry: ExecutionTelemetry | None,
) -> None:
    emit_workflow_event(telemetry, "node_started", node_id=node.id)
    if should_print_console(telemetry):
        execution_console(telemetry).rule(f"Node: {node.id} ({node.mode})")
    if node.mode == "input":
        execute_input_stage(
            node,
            output,
            runtime_context=runtime_context,
            telemetry=telemetry,
        )
    elif node.mode == "parallel":
        await execute_parallel_stage(
            node,
            output,
            runtime_context=runtime_context,
            invoker=invoker,
            telemetry=telemetry,
        )
    else:
        await execute_sequential_stage(
            node,
            output,
            runtime_context=runtime_context,
            invoker=invoker,
            telemetry=telemetry,
        )
    stage_finalize_result = output.finalize_stage(
        node.id,
        findings_enabled=node.findings,
        task_specs=build_stage_task_specs(node),
    )
    emit_stage_finalize_logs(telemetry, stage_finalize_result)
    if should_print_console(telemetry):
        execution_console(telemetry).print(f"[green]✓[/] Node '{node.id}' complete\n")
