from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.architecture.ports.artifacts import StageFinalizeResult
from orchestrator_cli.artifacts.workspace_node_state import (
    build_node_workspace_descriptor,
)
from orchestrator_cli.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    ArtifactDescriptor,
    NodeState,
)
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)

from .common import (
    ExecutionTelemetry,
    RuntimeEventContext,
    emit_runtime_log,
    emit_workflow_event,
)


def write_successful_node_state(
    node: PreflightExecutionNode,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    workflow_identity: str,
    finalize_result: StageFinalizeResult,
) -> None:
    output.write_node_success_state(
        NodeState(
            run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
            plan_schema_version=plan.plan_schema_version,
            workflow_identity=workflow_identity,
            workflow_name=plan.workflow_name,
            workflow_signature=plan.workflow_signature,
            run_id=output.run_id,
            run_key_name=output.run_key_name,
            node_id=node.id,
            completed_at=datetime.now().isoformat(),
            artifacts=_descriptors_for_result(output, finalize_result),
            generated_files=_generated_file_descriptors(output, finalize_result),
            workspace=build_node_workspace_descriptor(node, plan, output),
        )
    )


def emit_resumed_node_events(
    node_id: str,
    telemetry: ExecutionTelemetry | None,
) -> None:
    emit_workflow_event(telemetry, "node_started", node_id=node_id)
    emit_runtime_log(
        telemetry,
        level="info",
        message=f"Node '{node_id}' resumed from validated artifacts.",
        operation="node_resumed",
        context=RuntimeEventContext(node_id=node_id),
    )
    emit_workflow_event(telemetry, "node_finished", node_id=node_id)


def _descriptors_for_result(
    output: ArtifactStorePort,
    finalize_result: StageFinalizeResult,
) -> list[ArtifactDescriptor]:
    descriptors = [
        _descriptor("output", output.results_dir, finalize_result.result_file)
    ]
    if finalize_result.findings_file is not None:
        descriptors.append(
            _descriptor("findings", output.results_dir, finalize_result.findings_file)
        )
    return descriptors


def _generated_file_descriptors(
    output: ArtifactStorePort,
    finalize_result: StageFinalizeResult,
) -> list[ArtifactDescriptor]:
    return [
        _descriptor("generated_file", output.results_dir, generated_file)
        for generated_file in finalize_result.generated_files
    ]


def _descriptor(kind: str, root: Path, path: Path) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        kind=kind,
        relative_path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        size_bytes=path.stat().st_size,
    )
