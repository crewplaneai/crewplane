from __future__ import annotations

from collections.abc import Mapping

from crewplane.architecture.contracts import JsonObject
from crewplane.core.workflow.keywords import ProviderRole

from ..models import (
    PreflightCompilationPreview,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceFileLocator,
    WorkspaceFileSourceClass,
    WorkspaceFileTarget,
    WorkspaceSourceSnapshot,
)

WorkspacePlan = PreflightCompilationPreview | PreflightExecutionPlan


def workspace_observability_descriptor(plan: WorkspacePlan) -> JsonObject | None:
    if not workspace_enabled(plan):
        return None
    descriptor: JsonObject = {
        "enabled": True,
        "worktree_contract": workspace_contract(plan),
        "source": workspace_source_descriptor(plan.workspace_source),
        "nodes": [
            node_workspace_descriptor(node, plan.workspace_file_locators)
            for node in plan.nodes
            if node.workspace_policy is not None and node.workspace_policy.enabled
        ],
        "rendered_files": rendered_file_summary(plan.workspace_file_locators),
        "cleanup": cleanup_descriptor(plan.runtime_config_snapshot),
    }
    invoker = invoker_workspace_descriptor(plan.runtime_config_snapshot)
    if invoker is not None:
        descriptor["invoker"] = invoker
    return descriptor


def workspace_enabled(plan: WorkspacePlan) -> bool:
    return plan.workspace_source is not None or any(
        node.workspace_policy is not None and node.workspace_policy.enabled
        for node in plan.nodes
    )


def workspace_contract(plan: WorkspacePlan) -> JsonObject | None:
    if plan.workspace_source is not None:
        return plan.workspace_source.worktree_contract.model_dump(mode="json")
    for node in plan.nodes:
        policy = node.workspace_policy
        if policy is not None:
            return policy.worktree_contract.model_dump(mode="json")
    return None


def workspace_source_descriptor(
    source: WorkspaceSourceSnapshot | None,
) -> JsonObject | None:
    if source is None:
        return None
    return {
        "object_format": source.object_format,
        "repo_id": source.repository_id,
        "run_base_commit": source.run_base_commit,
        "source_tree": source.source_tree,
        "git_version": source.git_version,
        "project_root_relative_path": source.project_root_relative_path,
        "clean_start": source.clean_start,
        "worktree_contract": source.worktree_contract.model_dump(mode="json"),
        "local_config_policy": source.local_config_policy,
        "filesystem_capabilities": source.filesystem_capabilities,
    }


def node_workspace_descriptor(
    node: PreflightExecutionNode,
    locators: list[WorkspaceFileLocator],
) -> JsonObject:
    policy = node.workspace_policy
    if policy is None:
        raise ValueError("Workspace node descriptor requires a workspace policy.")
    node_locators = [locator for locator in locators if locator.node_id == node.id]
    return {
        "node_id": node.id,
        "mode": node.mode,
        "logical_worktree_name": policy.logical_worktree_name,
        "kind": policy.declaration_kind,
        "source_kind": policy.source_kind,
        "source_node_id": policy.source_node_id,
        "clean_start": policy.clean_start,
        "materialization": policy.materialization,
        "lineage_producer": policy.lineage_producer,
        "writable": policy.writable,
        "setup_profile": policy.setup.profile_name
        if policy.setup is not None
        else None,
        "branch_export": policy.branch_export.model_dump(mode="json"),
        "result": node_result_descriptor(node),
        "workspace_file_locator_count": len(node_locators),
        "runtime_dynamic_locator_count": sum(
            locator.source_class == WorkspaceFileSourceClass.RUNTIME_DYNAMIC
            for locator in node_locators
        ),
    }


def rendered_file_summary(locators: list[WorkspaceFileLocator]) -> JsonObject:
    return {
        "locator_count": len(locators),
        "project_initial": sum(
            locator.source_class == WorkspaceFileSourceClass.PROJECT_INITIAL
            for locator in locators
        ),
        "runtime_dynamic": sum(
            locator.source_class == WorkspaceFileSourceClass.RUNTIME_DYNAMIC
            for locator in locators
        ),
        "input_output": sum(
            locator.target == WorkspaceFileTarget.INPUT_OUTPUT for locator in locators
        ),
        "executor_prompt": sum(
            locator.target == WorkspaceFileTarget.EXECUTOR_PROMPT
            for locator in locators
        ),
        "reviewer_prompt": sum(
            locator.target == WorkspaceFileTarget.REVIEWER_PROMPT
            for locator in locators
        ),
    }


def invoker_workspace_descriptor(snapshot: object) -> JsonObject | None:
    payload = runtime_snapshot_payload(snapshot)
    invoker = payload.get("invoker") if payload is not None else None
    if not isinstance(invoker, Mapping):
        return None
    capabilities = invoker.get("capabilities")
    workspace = (
        capabilities.get("workspace") if isinstance(capabilities, Mapping) else None
    )
    if not isinstance(workspace, Mapping):
        return None
    descriptor: JsonObject = {
        "implementation": string_value(invoker.get("implementation")),
        "honors_cwd": bool_value(workspace.get("honors_cwd")),
        "launch_mode": string_value(workspace.get("launch_mode")),
        "controlled_child_environment": bool_value(
            workspace.get("controlled_child_environment")
        ),
    }
    return descriptor


def cleanup_descriptor(snapshot: object) -> JsonObject:
    payload = runtime_snapshot_payload(snapshot)
    workspace = payload.get("workspace") if payload is not None else None
    if not isinstance(workspace, Mapping):
        return {
            "cleanup_on_success": None,
            "cache_root_configured": False,
        }
    return {
        "cleanup_on_success": bool_value(workspace.get("cleanup_on_success")),
        "cache_root_configured": isinstance(workspace.get("cache_root"), str),
    }


def runtime_snapshot_payload(snapshot: object) -> Mapping[str, object] | None:
    if snapshot is None:
        return None
    if isinstance(snapshot, Mapping):
        return snapshot
    model_dump = getattr(snapshot, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="json")
        return payload if isinstance(payload, Mapping) else None
    return None


def effective_materialization(node: PreflightExecutionNode) -> str:
    policy = node.workspace_policy
    if policy is None:
        return "project_root"
    return policy.materialization


def workspace_lineage_producer(node: PreflightExecutionNode) -> bool:
    policy = node.workspace_policy
    return (
        policy is not None
        and policy.enabled
        and policy.lineage_producer
        and node.mode != "input"
        and any(
            provider.role == ProviderRole.EXECUTOR for provider in node.provider_records
        )
    )


def node_result_descriptor(node: PreflightExecutionNode) -> JsonObject:
    policy = node.workspace_policy
    if policy is None or not policy.enabled:
        return {"capture": "disabled", "bundle": "not_applicable"}
    if node.mode == "input":
        return {"capture": "static_file", "bundle": "not_applicable"}
    if policy.lineage_producer and workspace_lineage_producer(node):
        return {
            "capture": "deterministic_commit_tree",
            "bundle": "exported_on_success",
        }
    if policy.declaration_kind == "worktree":
        return {"capture": "discarded_drift_summary", "bundle": "not_applicable"}
    return {"capture": "discarded_snapshot_drift", "bundle": "not_applicable"}


def string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def bool_value(value: object) -> bool | None:
    return value if isinstance(value, bool) else None
