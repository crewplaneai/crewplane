from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.artifacts.workspace.node_state import (
    refresh_node_workspace_descriptor,
)
from orchestrator_cli.core.preflight.models import PreflightExecutionPlan
from orchestrator_cli.runtime.workspace.worktree.ref_cleanup import (
    cleanup_plan_workspace_refs,
)

from ..common import (
    ExecutionTelemetry,
    NodeStatus,
    RuntimeEventContext,
    emit_runtime_log,
    safe_error_message,
)


async def cleanup_successful_workspace_run_refs(
    plan: PreflightExecutionPlan,
    telemetry: ExecutionTelemetry | None,
) -> int:
    try:
        removed_ref_count = await asyncio.to_thread(cleanup_plan_workspace_refs, plan)
    except Exception as exc:
        emit_cleanup_errors(telemetry, "workspace_ref_cleanup", (exc,))
        return 0
    if removed_ref_count:
        emit_runtime_log(
            telemetry,
            level="info",
            message=(f"Removed {removed_ref_count} run-owned workspace Git ref(s)."),
            operation="workspace_ref_cleanup",
            context=RuntimeEventContext(),
            attributes={
                "run_key_name": plan.run_key_name,
                "removed_ref_count": removed_ref_count,
            },
        )
    return removed_ref_count


def emit_cleanup_errors(
    telemetry: ExecutionTelemetry | None,
    operation: str,
    errors: tuple[Exception, ...],
) -> None:
    if not errors:
        return
    emit_runtime_log(
        telemetry,
        level="warning",
        message=f"{operation} cleanup failed ({len(errors)} error(s)).",
        operation=operation,
        context=RuntimeEventContext(),
        attributes={
            "error_count": len(errors),
            "first_error": safe_error_message(errors[0]),
        },
    )


async def refresh_workspace_node_manifests_for_state_paths(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    statuses: dict[str, NodeStatus],
    updated_state_paths: tuple[Path, ...],
    telemetry: ExecutionTelemetry | None,
) -> None:
    failures = await asyncio.to_thread(
        workspace_manifest_refresh_failures_for_state_paths,
        plan,
        output,
        statuses,
        updated_state_paths,
    )
    emit_workspace_manifest_refresh_failures(telemetry, failures)


def workspace_manifest_refresh_failures_for_state_paths(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    statuses: dict[str, NodeStatus],
    updated_state_paths: tuple[Path, ...],
) -> tuple[tuple[str, Exception], ...]:
    if not updated_state_paths:
        return ()
    return workspace_manifest_refresh_failures(
        plan,
        output,
        statuses,
        node_ids_for_state_paths(plan, output, updated_state_paths),
    )


async def refresh_workspace_node_manifests(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    statuses: dict[str, NodeStatus],
    node_ids: set[str],
    telemetry: ExecutionTelemetry | None,
) -> None:
    failures = await asyncio.to_thread(
        workspace_manifest_refresh_failures,
        plan,
        output,
        statuses,
        node_ids,
    )
    emit_workspace_manifest_refresh_failures(telemetry, failures)


def workspace_manifest_refresh_failures(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    statuses: dict[str, NodeStatus],
    node_ids: set[str],
) -> tuple[tuple[str, Exception], ...]:
    if not node_ids:
        return ()
    nodes_by_id = {node.id: node for node in plan.nodes}
    failures: list[tuple[str, Exception]] = []
    for node_id in sorted(node_ids):
        if statuses.get(node_id) != "succeeded":
            continue
        node = nodes_by_id[node_id]
        try:
            refresh_node_workspace_descriptor(node, plan, output)
        except Exception as exc:
            failures.append((node_id, exc))
    return tuple(failures)


def emit_workspace_manifest_refresh_failures(
    telemetry: ExecutionTelemetry | None,
    failures: tuple[tuple[str, Exception], ...],
) -> None:
    for node_id, exc in failures:
        emit_runtime_log(
            telemetry,
            level="warning",
            message=(
                f"Workspace manifest refresh failed for node '{node_id}': "
                f"{safe_error_message(exc)}"
            ),
            operation="workspace_manifest_refresh",
            context=RuntimeEventContext(node_id=node_id),
        )


def node_ids_for_state_paths(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    updated_state_paths: tuple[Path, ...],
) -> set[str]:
    resolved_paths = {path.resolve(strict=False) for path in updated_state_paths}
    node_ids: set[str] = set()
    for node in plan.nodes:
        stage_path = node.artifact_contract.stage_path
        if stage_path is None:
            continue
        stage_dir = (output.stages_dir / stage_path).resolve(strict=False)
        if any(path.is_relative_to(stage_dir) for path in resolved_paths):
            node_ids.add(node.id)
    return node_ids
