from __future__ import annotations

from crewplane.core.workflow.models import WorkflowNode

from .compile_state import PreflightCompileOptions
from .models import (
    DependencyEdge,
    PreflightExecutionNode,
    RenderPlan,
    StaticResource,
    TokenCatalogEntry,
    WorkspaceFileLocator,
    WorkspaceSourceSnapshot,
)
from .runtime_config import (
    RuntimeConfigSnapshot,
    runtime_config_signature,
    workspace_signature_payload,
)
from .serialization import to_json_safe
from .signatures import signature_for_payload
from .source import PreflightWorkflowSource


def workflow_signature(
    source: PreflightWorkflowSource,
    options: PreflightCompileOptions,
    runtime_snapshot: RuntimeConfigSnapshot,
    render_plans: list[RenderPlan],
    static_resources: list[StaticResource],
    workspace_file_locators: list[WorkspaceFileLocator],
    token_catalog: list[TokenCatalogEntry],
    dependency_graph: list[DependencyEdge],
    nodes: list[PreflightExecutionNode],
    value_fingerprints: list[dict[str, str]],
) -> str:
    workflow = source.workflow
    semantic_workflow = semantic_workflow_payload(source.composed_workflow)
    return signature_for_payload(
        {
            "composed_workflow": semantic_workflow,
            "dependency_graph": dependency_graph,
            "effective_runtime_config_signature": runtime_snapshot.effective_runtime_config_signature,
            "nodes": semantic_node_payloads(nodes),
            "project_root": options.project_root.resolve().as_posix(),
            "referenced_workflows": semantic_referenced_workflows(source),
            "render_plans": render_plans,
            "static_resources": static_resources,
            "token_catalog": token_catalog,
            "value_fingerprints": value_fingerprints,
            "workspace_file_locators": workspace_file_locators,
            "workspace_source": workspace_source_signature_payload(
                options.workspace_source_snapshot
            ),
            "workflow_semantic_sha256": signature_for_payload(semantic_workflow),
            "workflow_name": workflow.name,
        }
    )


def effective_runtime_config_signature_for_plan(
    runtime_snapshot: RuntimeConfigSnapshot,
    nodes: list[PreflightExecutionNode],
) -> str:
    return runtime_config_signature(
        runtime_snapshot,
        semantic_workspace_runtime_payload(runtime_snapshot, nodes),
    )


def semantic_workspace_runtime_payload(
    runtime_snapshot: RuntimeConfigSnapshot,
    nodes: list[PreflightExecutionNode],
) -> dict[str, object]:
    policies = [
        node.workspace_policy
        for node in nodes
        if node.workspace_policy is not None and node.workspace_policy.enabled
    ]
    if not policies:
        return {"enabled": False}

    payload = workspace_signature_payload(runtime_snapshot.workspace)
    if any(policy.setup is not None and policy.setup.commands for policy in policies):
        payload["setup_timeout_seconds"] = (
            runtime_snapshot.workspace.setup_timeout_seconds
        )
    return payload


def template_hash(node: WorkflowNode) -> str:
    normalized_segments = [
        segment.content.replace("\r\n", "\n").replace("\r", "\n")
        for segment in node.prompt_segments
    ]
    return signature_for_payload({"segments": normalized_segments})


def workspace_source_signature_payload(
    snapshot: WorkspaceSourceSnapshot | None,
) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "clean_start": snapshot.clean_start,
        "object_format": snapshot.object_format,
        "project_root_relative_path": snapshot.project_root_relative_path,
        "repository_id": snapshot.repository_id,
        "run_base_commit": snapshot.run_base_commit,
        "source_tree": snapshot.source_tree,
        "worktree_contract": snapshot.worktree_contract.model_dump(mode="json"),
    }


def semantic_referenced_workflows(
    source: PreflightWorkflowSource,
) -> list[dict[str, str]]:
    return [{"path": record.path.as_posix()} for record in source.referenced_workflows]


def semantic_workflow_payload(payload: object) -> object:
    semantic_payload = to_json_safe(payload)
    if not isinstance(semantic_payload, dict):
        return semantic_payload

    worktrees = semantic_payload.get("worktrees")
    if not isinstance(worktrees, dict):
        return semantic_payload

    semantic_payload = dict(semantic_payload)
    semantic_payload["worktrees"] = {
        name: semantic_worktree_declaration(declaration)
        for name, declaration in worktrees.items()
    }
    return semantic_payload


def semantic_worktree_declaration(declaration: object) -> object:
    payload = to_json_safe(declaration)
    if not isinstance(payload, dict):
        return payload
    return {
        key: value
        for key, value in payload.items()
        if key not in {"branch_name", "create_branch"}
    }


def semantic_node_payloads(
    nodes: list[PreflightExecutionNode],
) -> list[object]:
    payloads: list[object] = []
    for node in nodes:
        payload = to_json_safe(node)
        if isinstance(payload, dict) and isinstance(
            payload.get("workspace_policy"),
            dict,
        ):
            workspace_policy = dict(payload["workspace_policy"])
            workspace_policy.pop("branch_export", None)
            payload["workspace_policy"] = workspace_policy
        payloads.append(payload)
    return payloads
