from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts import (
    AgentInvoker,
    EventType,
    NodeArtifactRequest,
)
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.architecture.ports.artifacts import StageFinalizeResult
from crewplane.artifacts.results.findings import FindingsExtractionError
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import PreflightExecutionNode

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
from ..errors import NodeExecutionError
from ..input import execute_input_stage
from ..parallel import execute_parallel_stage
from ..publication_registry import RuntimePublicationRegistry
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
    emit_workflow_event(telemetry, EventType.NODE_STARTED, node_id=node.id)
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
    publications = runtime_context.runtime_publications
    with publications.transaction():
        try:
            stage_finalize_result = output.finalize_node(
                NodeArtifactRequest(node.id, node.artifact_contract),
                findings_enabled=node.findings,
                task_specs=build_stage_task_specs(node),
                generated_file_detection_enabled=generated_file_detection_enabled(node),
                generated_file_workspace_roots=(
                    runtime_context.generated_file_workspaces.roots_for_node(node.id)
                ),
            )
        except FindingsExtractionError as exc:
            raise NodeExecutionError(str(exc)) from exc
        _register_stage_publications(publications, stage_finalize_result)
    emit_stage_finalize_logs(telemetry, stage_finalize_result)
    cleanup_errors = (
        await runtime_context.generated_file_workspaces.cleanup_node_best_effort_async(
            node.id,
        )
    )
    _emit_generated_file_workspace_cleanup_errors(telemetry, node.id, cleanup_errors)
    with publications.transaction():
        node_state_path = write_successful_node_state(
            node,
            runtime_context.plan,
            output,
            workflow_identity,
            stage_finalize_result,
        )
        _register_recoverable_publication(publications, node_state_path)
    if should_print_console(telemetry):
        execution_console(telemetry).print(f"[green]✓[/] Node '{node.id}' complete\n")


def generated_file_detection_enabled(node: PreflightExecutionNode) -> bool:
    return node.mode != "input"


def _register_stage_publications(
    publications: RuntimePublicationRegistry,
    result: StageFinalizeResult,
) -> None:
    reserved_paths = (result.result_file, result.findings_file)
    for path in reserved_paths:
        if path is not None:
            _register_recoverable_publication(publications, path)
    for path in result.generated_files:
        if path not in reserved_paths:
            publications.publish(
                path,
                file_size_and_sha256(path),
                recovery_source=path,
            )


def _register_recoverable_publication(
    publications: RuntimePublicationRegistry,
    path: Path,
) -> None:
    publications.publish(
        path,
        file_size_and_sha256(path),
        recovery_source=path,
    )


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
