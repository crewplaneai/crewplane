from __future__ import annotations

from datetime import datetime

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    RunManifest,
    RunStatus,
)
from orchestrator_cli.core.preflight import PreflightExecutionPlan
from orchestrator_cli.core.preflight.source import PreflightWorkflowSource
from orchestrator_cli.core.preflight.workspace_observability import (
    workspace_observability_descriptor,
)

from .context import WorkflowRunContext


def write_running_run_manifest(
    output: ArtifactStorePort,
    manifest: RunManifest,
) -> None:
    output.write_run_manifest(manifest)


def finalize_run_manifest(
    output: ArtifactStorePort,
    status: RunStatus,
    failure_message: str | None = None,
    cancel_reason: str | None = None,
) -> None:
    output.update_run_manifest_status(
        status=status,
        completed_at=datetime.now().isoformat(),
        failure_message=failure_message,
        cancel_reason=cancel_reason,
    )


def print_duplicate_context_message(
    context: WorkflowRunContext,
    source_run_id: str | None = None,
) -> None:
    source_label = f" in run {source_run_id}" if source_run_id else ""
    context.console.print(
        "[yellow]Identical context detected for workflow "
        f"'{context.workflow.name}'. Previous outputs exist{source_label}. "
        "Use `--force` to run anyway.[/]"
    )


def print_resume_context_message(
    context: WorkflowRunContext,
    resumed_node_count: int,
    source_run_id: str,
) -> None:
    context.console.print(
        "[yellow]Resuming workflow "
        f"'{context.workflow.name}' from {resumed_node_count} validated node "
        f"boundary(s) in run {source_run_id}.[/]"
    )


def build_run_manifest_from_plan(
    plan: PreflightExecutionPlan,
    source: PreflightWorkflowSource,
    workflow_identity: str,
    resumed_nodes: tuple[str, ...] = (),
    resume_source_run_id: str | None = None,
    resume_source_run_key_name: str | None = None,
) -> RunManifest:
    return RunManifest(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        plan_schema_version=plan.plan_schema_version,
        workflow_identity=workflow_identity,
        workflow_name=plan.workflow_name,
        workflow_signature=plan.workflow_signature,
        run_id=plan.run_id,
        run_key_name=plan.run_key_name,
        started_at=datetime.now().isoformat(),
        status="running",
        effective_runtime_config_signature=plan.effective_runtime_config_signature,
        preflight_plan_path="preflight/execution-plan.json",
        preflight_manifest_path="preflight/manifest.json",
        runtime_config_snapshot_path="preflight/runtime-config-snapshot.json",
        runtime_config_snapshot=plan.runtime_config_snapshot,
        workflow_source=source.workflow_content,
        composed_workflow=source.composed_workflow,
        referenced_workflows=source.referenced_workflow_payloads(),
        workspace=workspace_observability_descriptor(plan),
        resumed_nodes=list(resumed_nodes),
        resume_source_run_id=resume_source_run_id,
        resume_source_run_key_name=resume_source_run_key_name,
    )
