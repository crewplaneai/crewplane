from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from orchestrator_cli.architecture.loader import instantiate_adapter
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.bootstrap import RuntimeConfigSnapshotBuildResult
from orchestrator_cli.core.config import Settings
from orchestrator_cli.core.preflight import PreflightExecutionPlan
from orchestrator_cli.core.preflight.source import PreflightWorkflowSource

from ..manifest import ExecutionManifest
from .context import WorkflowRunContext

ManifestStatus = Literal["succeeded", "failed"]


@dataclass(frozen=True)
class PreparedExecutionManifest:
    workflow_signature: str
    manifest: ExecutionManifest


def finalize_manifest(
    output: ArtifactStorePort,
    workflow_signature: str,
    manifest: ExecutionManifest,
    status: ManifestStatus,
) -> None:
    manifest["completed_at"] = datetime.now().isoformat()
    manifest["status"] = status
    output.write_manifest(workflow_signature, manifest)


def print_duplicate_context_message(context: WorkflowRunContext) -> None:
    context.console.print(
        "[yellow]Identical context detected for workflow "
        f"'{context.workflow.name}'. Previous outputs exist. "
        "Use `--force` to run anyway.[/]"
    )


def workflow_signature_already_exists(
    context: WorkflowRunContext,
    snapshot_result: RuntimeConfigSnapshotBuildResult,
    workflow_signature: str,
    force: bool,
) -> bool:
    if force:
        return False

    settings = (
        context.config.settings if context.config.settings is not None else Settings()
    )
    artifacts_adapter = instantiate_adapter(
        "artifacts",
        settings.integrations.artifacts.implementation,
    )
    return artifacts_adapter.workflow_signature_exists(
        context.workflow.name,
        context.orchestrator_dir,
        snapshot_result.artifact_options,
        workflow_signature,
    )


def build_manifest_from_plan(
    plan: PreflightExecutionPlan,
    source: PreflightWorkflowSource,
) -> ExecutionManifest:
    return {
        "workflow_signature": plan.workflow_signature,
        "workflow_name": plan.workflow_name,
        "generated_at": datetime.now().isoformat(),
        "workflow": source.workflow_content,
        "composed_workflow": source.composed_workflow,
        "referenced_workflows": source.referenced_workflow_payloads(),
        "runtime_config_snapshot": plan.runtime_config_snapshot,
        "preflight_plan": "preflight/execution-plan.json",
        "effective_runtime_config_signature": plan.effective_runtime_config_signature,
    }
