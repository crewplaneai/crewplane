"""Validate persisted preflight workspace file locator contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import plan_contract_records

if TYPE_CHECKING:
    from .models import (
        PreflightExecutionNode,
        PreflightExecutionPlan,
        RenderPlan,
        RenderStream,
        WorkspaceFileLocator,
        WorkspaceSelectionRecord,
    )


def validate_workspace_locator_relationships(
    plan: PreflightExecutionPlan,
    nodes_by_id: dict[str, PreflightExecutionNode],
    render_plans_by_id: dict[str, RenderPlan],
) -> None:
    locators_by_id = plan_contract_records.unique_records(
        plan.workspace_file_locators,
        "locator_id",
        "workspace file locator",
    )
    plan_contract_records.unique_records(
        plan.workspace_file_locators,
        "occurrence_id",
        "workspace file occurrence",
    )
    unknown_node_locators = sorted(
        locator.locator_id
        for locator in plan.workspace_file_locators
        if locator.node_id not in nodes_by_id
    )
    if unknown_node_locators:
        raise ValueError(
            "Persisted workspace locators reference unknown nodes: "
            + ", ".join(unknown_node_locators)
        )
    referenced: dict[str, int] = {locator_id: 0 for locator_id in locators_by_id}
    for node in plan.nodes:
        _validate_node_workspace_locator(node, locators_by_id, referenced)
        if node.render_plan_id is None:
            continue
        render_plan = render_plans_by_id[node.render_plan_id]
        for stream in render_plan.streams:
            _validate_workspace_locator_stream(
                node.id,
                stream,
                locators_by_id,
                referenced,
            )
    unreferenced = sorted(
        locator_id for locator_id, count in referenced.items() if count != 1
    )
    if unreferenced:
        raise ValueError(
            "Persisted workspace locators must each have exactly one owner: "
            + ", ".join(unreferenced)
        )


def _validate_workspace_locator_stream(
    node_id: str,
    stream: RenderStream,
    locators_by_id: dict[str, WorkspaceFileLocator],
    referenced: dict[str, int],
) -> None:
    for fragment in stream.fragments:
        if fragment.kind != "workspace_file_locator":
            continue
        locator_id = (fragment.locator or {}).get("locator_id")
        locator = locators_by_id.get(locator_id or "")
        if locator is None:
            raise ValueError(
                f"Persisted node '{node_id}' references unknown workspace "
                f"locator '{locator_id}'."
            )
        if locator.node_id != node_id:
            raise ValueError("Workspace locator node identity is inconsistent.")
        expected_target = f"{stream.target_role.value}_prompt"
        if locator.target.value != expected_target:
            raise ValueError("Workspace locator target conflicts with its stream.")
        referenced[locator_id or ""] += 1


def _validate_node_workspace_locator(
    node: PreflightExecutionNode,
    locators_by_id: dict[str, WorkspaceFileLocator],
    referenced: dict[str, int],
) -> None:
    input_locator_id = node.input_workspace_file_locator_id
    if input_locator_id is not None:
        locator = locators_by_id.get(input_locator_id)
        if locator is None or locator.node_id != plan_contract_records.node_id(node):
            raise ValueError(
                f"Persisted input node '{plan_contract_records.node_id(node)}' "
                "references an invalid workspace locator."
            )
        if locator.target.value != "input_output":
            raise ValueError("Input node workspace locator has the wrong target.")
        referenced[input_locator_id] += 1
    node_locators = [
        locator
        for locator in locators_by_id.values()
        if locator.node_id == plan_contract_records.node_id(node)
    ]
    managed_locators = [
        locator for locator in node_locators if locator.target.value != "input_output"
    ]
    if not managed_locators:
        return
    workspace_policy = node.workspace_policy
    if workspace_policy is None or not workspace_policy.enabled:
        raise ValueError(
            f"Persisted node '{plan_contract_records.node_id(node)}' has workspace "
            "locators without an enabled workspace policy."
        )
    for locator in managed_locators:
        expected_source_class = _expected_workspace_locator_source_class(
            workspace_policy,
            locator,
        )
        if locator.source_class.value != expected_source_class:
            raise ValueError(
                f"Persisted node '{plan_contract_records.node_id(node)}' workspace "
                f"locator '{locator.locator_id}' source class conflicts with its "
                "target and workspace lineage."
            )


def _expected_workspace_locator_source_class(
    workspace_policy: WorkspaceSelectionRecord,
    locator: WorkspaceFileLocator,
) -> str:
    if workspace_policy.declaration_kind != "worktree":
        return "project_initial"
    if (
        locator.target.value == "reviewer_prompt"
        or workspace_policy.source_kind == "node"
    ):
        return "runtime_dynamic"
    return "project_initial_then_candidate"
