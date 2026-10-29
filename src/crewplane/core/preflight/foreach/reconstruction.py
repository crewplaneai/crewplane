from __future__ import annotations

import heapq

from crewplane.core.workflow.collections import (
    collection_reference,
    validate_collection,
)
from crewplane.core.workspace.policy import generated_branch_name

from .. import plan_contract_records, plan_contract_shape
from ..compiled_records import PreflightExecutionNode
from ..models import PreflightExecutionPlan
from ..signatures import signature_for_payload
from .expansion import (
    concrete_batch,
    expansion_directory,
    expansion_limit,
    scheduling_edge,
)
from .models import ExpansionBatch


def validate_frozen_templates(plan: PreflightExecutionPlan) -> None:
    from ..plan_contract import validate_execution_graph_semantics

    templates = plan_contract_records.unique_records(
        plan.foreach_templates, "group_id", "foreach template"
    )
    groups = {node.id: node for node in plan.nodes if node.mode == "foreach"}
    if templates.keys() != groups.keys():
        raise ValueError(
            "Every foreach node requires exactly one frozen child template"
        )
    if not templates:
        return
    expansion_limit(plan)
    execution = plan.runtime_config_snapshot.get("execution")
    concurrency = (
        execution.get("max_concurrent_nodes") if isinstance(execution, dict) else None
    )
    if type(concurrency) is not int or concurrency <= 0:
        raise ValueError("Foreach requires finite node concurrency")
    if sum(template.node_bound for template in templates.values()) > expansion_limit(
        plan
    ):
        raise ValueError("Frozen foreach bounds exceed max_expanded_nodes")
    for template in templates.values():
        if isinstance(template.items, list):
            validate_collection(template.items, template.max_items, template.group_id)
        else:
            collection_reference(template.items)
        if (
            template.namespace != f"{template.group_id}.template"
            or template.semantic_digest() != template.template_digest
        ):
            raise ValueError("Frozen child template identity changed")
        ids = plan_contract_records.unique_records(template.nodes, "id", "child node")
        plan_contract_records.validate_execution_order(template.execution_order, ids)
        slots = [
            node.id
            for node in template.nodes
            if node.input_item_slot and node.mode == "input"
        ]
        if slots != [template.item_node_id]:
            raise ValueError(
                "Frozen child requires exactly one declared item input slot"
            )
        if any(
            not node.id.startswith(f"{template.namespace}.")
            or node.mode == "foreach"
            or node.expansion is not None
            for node in template.nodes
        ):
            raise ValueError(
                "Frozen child contains invalid node identity, expansion, or nested foreach"
            )
        plan_contract_shape.validate_persisted_node_contracts(template.nodes)
    children = [node for template in templates.values() for node in template.nodes]
    nodes = [*plan.nodes, *children]
    inventory = plan.model_copy(
        update={
            "nodes": nodes,
            "execution_order": topological_order(nodes),
            "render_plans": [
                *plan.render_plans,
                *(
                    render
                    for template in templates.values()
                    for render in template.render_plans
                ),
            ],
            "workspace_file_locators": [
                *plan.workspace_file_locators,
                *(
                    locator
                    for template in templates.values()
                    for locator in template.workspace_file_locators
                ),
            ],
            "dependency_graph": [
                *plan.dependency_graph,
                *(
                    edge
                    for template in templates.values()
                    for edge in template.dependency_graph
                ),
            ],
        }
    )
    validate_execution_graph_semantics(inventory)


def apply_expansion(
    plan: PreflightExecutionPlan, batch: ExpansionBatch
) -> PreflightExecutionPlan:
    group_id = batch.state.group_id
    if group_id in plan.expansions:
        raise ValueError(f"Duplicate expansion '{group_id}'")
    template = next(
        (
            template
            for template in plan.foreach_templates
            if template.group_id == group_id
        ),
        None,
    )
    group = next((node for node in plan.nodes if node.id == group_id), None)
    if (
        template is None
        or group is None
        or batch != concrete_batch(template, batch.state, group.dependencies)
    ):
        raise ValueError("Expansion additions differ from the frozen child template")
    nodes = []
    for node in plan.nodes:
        if node.id == group_id:
            nodes.extend(batch.nodes)
            node = node.model_copy(
                update={
                    "dependencies": batch.group_dependencies,
                    "expansion": batch.state.provenance(),
                }
            )
        nodes.append(node)
    edges = [*plan.dependency_graph, *batch.dependency_graph]
    existing = {(edge.source_node, edge.target_node) for edge in edges}
    edges.extend(
        scheduling_edge(dependency, group_id)
        for dependency in batch.group_dependencies
        if (dependency, group_id) not in existing
    )
    updated = plan.model_copy(
        update={
            "nodes": nodes,
            "execution_order": topological_order(nodes),
            "dependency_graph": edges,
            "render_plans": [*plan.render_plans, *batch.render_plans],
            "workspace_file_locators": [
                *plan.workspace_file_locators,
                *batch.workspace_file_locators,
            ],
            "token_catalog": [*plan.token_catalog, *batch.token_catalog],
            "expansions": {**plan.expansions, group_id: batch.state},
        }
    )
    result = PreflightExecutionPlan.model_validate(updated)
    validate_branch_targets(result)
    return result


def topological_order(nodes: list[PreflightExecutionNode]) -> list[str]:
    order = {node.id: index for index, node in enumerate(nodes)}
    if len(order) != len(nodes):
        raise ValueError("Expansion contains colliding node identities")
    remaining = {node.id: len(node.dependencies) for node in nodes}
    dependents: dict[str, list[str]] = {node.id: [] for node in nodes}
    for node in nodes:
        for dependency in node.dependencies:
            if dependency not in dependents:
                raise ValueError(
                    f"Expansion references unknown dependency '{dependency}'"
                )
            dependents[dependency].append(node.id)
    ready = [
        (order[node_id], node_id) for node_id, count in remaining.items() if count == 0
    ]
    heapq.heapify(ready)
    result = []
    while ready:
        _, node_id = heapq.heappop(ready)
        result.append(node_id)
        for dependent in dependents[node_id]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                heapq.heappush(ready, (order[dependent], dependent))
    if len(result) != len(nodes):
        raise ValueError("Expansion introduces a dependency cycle")
    return result


def validate_expansion_provenance(plan: PreflightExecutionPlan) -> None:
    templates = {template.group_id: template for template in plan.foreach_templates}
    expected: dict[str, object] = {}
    generated_count = 0
    nodes = {node.id: node for node in plan.nodes}
    for group_id, state in plan.expansions.items():
        template = templates.get(group_id)
        if (
            template is None
            or state.group_id != group_id
            or state.template_digest != template.template_digest
            or state.workflow_signature != plan.workflow_signature
        ):
            raise ValueError(
                "Expansion does not match its frozen template and root signature"
            )
        root = expansion_directory(group_id)
        if (
            len(state.items) > template.max_items
            or state.collection.relative_path != f"{root}/items.json"
            or any(
                item.relative_path != f"{root}/items/{ordinal}.json"
                for ordinal, item in enumerate(state.items)
            )
        ):
            raise ValueError(
                "Expansion item count or locators differ from the compiled contract"
            )
        if state.digest != signature_for_payload(
            {
                "workflow_signature": state.workflow_signature,
                "template_digest": state.template_digest,
                "group_id": group_id,
                "collection": state.collection,
                "items": state.items,
                "caller_artifacts": state.caller_artifacts,
                "shared_bindings": template.shared_bindings,
            }
        ):
            raise ValueError("Expansion digest does not match its evidence")
        original = list(template.original_dependencies)
        if isinstance(template.items, str):
            source, kind = collection_reference(template.items)
            if source not in original:
                original.append(source)
        derived = concrete_batch(template, state, original)
        group = nodes[group_id]
        if set(group.dependencies) != set(derived.group_dependencies):
            raise ValueError(
                "Foreach completion gate differs from its instance terminals"
            )
        for node in derived.nodes:
            if nodes.get(node.id) != node:
                raise ValueError(
                    f"Generated node '{node.id}' differs from its frozen template"
                )
        expected[group_id] = state.provenance()
        for generated in derived.nodes:
            expected[generated.id] = generated.expansion
        generated_count += len(derived.nodes)
    if plan.expansions and generated_count > expansion_limit(plan):
        raise ValueError("Restored expansions exceed max_expanded_nodes")
    for node in plan.nodes:
        if node.expansion != expected.get(node.id):
            raise ValueError(f"Node '{node.id}' has invalid expansion provenance")
        if node.input_item_slot:
            raise ValueError(
                "Uninstantiated item slots cannot enter an executable graph"
            )


def validate_branch_targets(plan: PreflightExecutionPlan) -> None:
    targets: dict[str, str] = {}
    for node in plan.nodes:
        policy = node.workspace_policy
        if policy is None or not policy.branch_export.create_branch:
            continue
        logical = policy.logical_worktree_name or ""
        target = policy.branch_export.branch_name or generated_branch_name(
            plan.workflow_name, logical, plan.run_key_name
        )
        if target in targets and targets[target] != logical:
            raise ValueError(f"Expansion branch target collision: '{target}'")
        targets[target] = logical
