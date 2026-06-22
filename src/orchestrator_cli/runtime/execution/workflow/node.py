from __future__ import annotations

from orchestrator_cli.architecture.contracts import AgentInvoker
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight.models import PreflightExecutionNode

from ..common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    RuntimeEventContext,
    build_stage_task_specs,
    emit_runtime_log,
    emit_stage_finalize_logs,
    emit_workflow_event,
    execution_console,
    safe_error_message,
    should_print_console,
)
from ..input import execute_input_stage
from ..parallel import execute_parallel_stage
from ..resume import write_successful_node_state
from ..sequential import execute_sequential_stage


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
    workflow_identity: str,
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
        generated_file_detection_enabled=generated_file_detection_enabled(node),
        generated_file_workspace_roots=(
            runtime_context.generated_file_workspaces.roots_for_node(node.id)
        ),
    )
    emit_stage_finalize_logs(telemetry, stage_finalize_result)
    cleanup_errors = (
        await runtime_context.generated_file_workspaces.cleanup_node_best_effort_async(
            node.id,
        )
    )
    _emit_generated_file_workspace_cleanup_errors(telemetry, node.id, cleanup_errors)
    write_successful_node_state(
        node,
        runtime_context.plan,
        output,
        workflow_identity,
        stage_finalize_result,
    )
    if should_print_console(telemetry):
        execution_console(telemetry).print(f"[green]✓[/] Node '{node.id}' complete\n")


def generated_file_detection_enabled(node: PreflightExecutionNode) -> bool:
    return node.mode != "input"


def _emit_generated_file_workspace_cleanup_errors(
    telemetry: ExecutionTelemetry | None,
    node_id: str,
    errors: tuple[Exception, ...],
) -> None:
    if not errors:
        return
    emit_runtime_log(
        telemetry,
        level="warning",
        message=(
            "Generated-file workspace cleanup failed "
            f"({len(errors)} error(s)); workspace was retained for later cleanup."
        ),
        operation="generated_file_workspace_cleanup",
        context=RuntimeEventContext(node_id=node_id),
        attributes={
            "error_count": len(errors),
            "first_error": safe_error_message(errors[0]),
        },
    )
