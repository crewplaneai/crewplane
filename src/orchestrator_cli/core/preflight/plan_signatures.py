from __future__ import annotations

from orchestrator_cli.core.workflow_models import WorkflowNode

from .compile_state import PreflightCompileOptions
from .models import (
    DependencyEdge,
    PreflightExecutionNode,
    RenderPlan,
    StaticResource,
    TokenCatalogEntry,
)
from .runtime_config import RuntimeConfigSnapshot
from .signatures import signature_for_payload
from .source import PreflightWorkflowSource


def workflow_signature(
    source: PreflightWorkflowSource,
    options: PreflightCompileOptions,
    runtime_snapshot: RuntimeConfigSnapshot,
    render_plans: list[RenderPlan],
    static_resources: list[StaticResource],
    token_catalog: list[TokenCatalogEntry],
    dependency_graph: list[DependencyEdge],
    nodes: list[PreflightExecutionNode],
    value_fingerprints: list[dict[str, str]],
) -> str:
    workflow = source.workflow
    return signature_for_payload(
        {
            "composed_workflow": source.composed_workflow,
            "dependency_graph": dependency_graph,
            "effective_runtime_config_signature": runtime_snapshot.effective_runtime_config_signature,
            "nodes": nodes,
            "project_root": options.project_root.resolve().as_posix(),
            "referenced_workflows": source.referenced_workflow_payloads(),
            "render_plans": render_plans,
            "static_resources": static_resources,
            "token_catalog": token_catalog,
            "value_fingerprints": value_fingerprints,
            "workflow_content_sha256": signature_for_payload(
                {"content": source.workflow_content}
            ),
            "workflow_name": workflow.name,
        }
    )


def template_hash(node: WorkflowNode) -> str:
    normalized_segments = [
        segment.content.replace("\r\n", "\n").replace("\r", "\n")
        for segment in node.prompt_segments
    ]
    return signature_for_payload({"segments": normalized_segments})
