from __future__ import annotations

from typing import NotRequired, TypedDict

from orchestrator_cli.architecture.contracts import JsonObject
from orchestrator_cli.core.workflow_models import WorkflowPayload


class WorkflowReferenceRecord(TypedDict):
    path: str
    sha256: str


class ExecutionManifest(TypedDict):
    workflow_signature: str
    workflow_name: str
    generated_at: str
    workflow: str
    composed_workflow: WorkflowPayload
    referenced_workflows: list[WorkflowReferenceRecord]
    runtime_config_snapshot: JsonObject
    preflight_plan: str
    effective_runtime_config_signature: str
    completed_at: NotRequired[str]
    status: NotRequired[str]
    preflight: NotRequired[JsonObject]
