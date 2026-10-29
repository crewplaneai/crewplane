"""Validate frozen foreach contracts and reconstruct effective graphs without I/O."""

from __future__ import annotations

import heapq

from crewplane.core.expansion_state import ExpansionProvenance, ExpansionState
from crewplane.core.workflow.collections import (
    collection_reference,
    validate_collection,
)
from crewplane.core.workspace.policy import generated_branch_name

from .. import plan_contract_records, plan_contract_shape
from ..compiled_records import DependencyEdge, PreflightExecutionNode
from ..models import CompiledForeach, PreflightExecutionPlan
from ..plan_contract import validate_execution_graph_semantics
from .expansion import (
    concrete_batch,
    expansion_directory,
    expansion_limit,
    scheduling_edge,
)
from .expansion_identity import expansion_digest
from .models import ExpansionBatch


def validate_frozen_templates(plan: PreflightExecutionPlan) -> None:
    """Validate persisted child templates without changing the plan or its records.

    Requires structurally valid plan records. Check template/group ownership,
    limits, and child contracts in stored order, then the combined graph.

    Raises:
        ValueError: The first ownership, budget, template, or graph violation.
    """
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
    _validate_frozen_limits(plan)
    for template in templates.values():
        _validate_frozen_template(template)
    validate_execution_graph_semantics(_frozen_template_inventory(plan))


def _validate_frozen_limits(plan: PreflightExecutionPlan) -> None:
    expansion_limit(plan)
    execution = plan.runtime_config_snapshot.get("execution")
    concurrency = (
        execution.get("max_concurrent_nodes") if isinstance(execution, dict) else None
    )
    if type(concurrency) is not int or concurrency <= 0:
        raise ValueError("Foreach requires finite node concurrency")
    if sum(
        template.node_bound for template in plan.foreach_templates
    ) > expansion_limit(plan):
        raise ValueError("Frozen foreach bounds exceed max_expanded_nodes")


def _validate_frozen_template(template: CompiledForeach) -> None:
    if isinstance(template.items, list):
        validate_collection(template.items, template.max_items, template.group_id)
    else:
        if template.max_items <= 0:
            raise ValueError(
                f"Foreach '{template.group_id}' artifact reference requires positive max_items"
            )
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
        raise ValueError("Frozen child requires exactly one declared item input slot")
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


def _frozen_template_inventory(
    plan: PreflightExecutionPlan,
) -> PreflightExecutionPlan:
    templates = plan.foreach_templates
    children = [node for template in templates for node in template.nodes]
    nodes = [*plan.nodes, *children]
    return plan.model_copy(
        update={
            "nodes": nodes,
            "execution_order": topological_order(nodes),
            "render_plans": [
                *plan.render_plans,
                *(render for template in templates for render in template.render_plans),
            ],
            "workspace_file_locators": [
                *plan.workspace_file_locators,
                *(
                    locator
                    for template in templates
                    for locator in template.workspace_file_locators
                ),
            ],
            "dependency_graph": [
                *plan.dependency_graph,
                *(edge for template in templates for edge in template.dependency_graph),
            ],
        }
    )


def apply_expansion(
    plan: PreflightExecutionPlan, batch: ExpansionBatch
) -> PreflightExecutionPlan:
    """Return a validated effective plan for a batch derived from a frozen child.

    Requires a valid plan and batch evidence. Insert batch nodes before their
    group; retain edge order and append missing gate edges in dependency order.
    Neither input is mutated. New inventory containers share unchanged records,
    batch records/state, and nested data; the copied group shares gate dependencies.
    Artifacts remain unpublished.

    Raises:
        ValueError: Duplicate expansion, mismatched additions, invalid evidence
            or graph, then branch collisions, in that validation order.
    """
    _validate_expansion_batch(plan, batch)
    nodes = _expanded_nodes(plan, batch)
    edges = _expanded_dependency_graph(plan, batch)
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
            "expansions": {**plan.expansions, batch.state.group_id: batch.state},
        }
    )
    result = PreflightExecutionPlan.model_validate(updated)
    validate_branch_targets(result)
    return result


def _validate_expansion_batch(
    plan: PreflightExecutionPlan, batch: ExpansionBatch
) -> None:
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


def _expanded_nodes(
    plan: PreflightExecutionPlan, batch: ExpansionBatch
) -> list[PreflightExecutionNode]:
    group_id = batch.state.group_id
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
    return nodes


def _expanded_dependency_graph(
    plan: PreflightExecutionPlan, batch: ExpansionBatch
) -> list[DependencyEdge]:
    group_id = batch.state.group_id
    edges = [*plan.dependency_graph, *batch.dependency_graph]
    existing = {(edge.source_node, edge.target_node) for edge in edges}
    edges.extend(
        scheduling_edge(dependency, group_id)
        for dependency in batch.group_dependencies
        if (dependency, group_id) not in existing
    )
    return edges


def topological_order(nodes: list[PreflightExecutionNode]) -> list[str]:
    """Return node IDs in dependency order without mutating the supplied records.

    Among currently ready nodes, prefer the earliest input position, including
    newly ready nodes. An empty input returns an empty list. Repeated dependency
    entries are counted and released individually; this checks graph topology,
    not the full persisted-node contract.

    Raises:
        ValueError: Colliding IDs, then unknown dependencies in input order,
            then dependency cycles.
    """
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
    """Verify stored expansions and node provenance without reading artifact bytes.

    Requires structurally valid records and a group node for each expansion.
    Check expansions in mapping order: identity, evidence, gate, generated nodes;
    then the cumulative budget and provenance/slots in node order. Inputs are
    unchanged, and caller evidence is checked through the digest only.

    Raises:
        ValueError: The first inconsistent expansion, budget, provenance, or slot.
        KeyError: An expansion's group node is absent from the supplied inventory.
    """
    templates = {template.group_id: template for template in plan.foreach_templates}
    expected: dict[str, ExpansionProvenance | None] = {}
    generated_count = 0
    nodes = {node.id: node for node in plan.nodes}
    for group_id, state in plan.expansions.items():
        template = _validate_expansion_identity(plan, group_id, templates.get(group_id))
        _validate_expansion_evidence(template, state)
        derived = _reconstruct_expansion(template, state)
        _validate_generated_nodes(nodes, derived)
        expected[group_id] = state.provenance()
        for generated in derived.nodes:
            expected[generated.id] = generated.expansion
        generated_count += len(derived.nodes)
    if plan.expansions and generated_count > expansion_limit(plan):
        raise ValueError("Restored expansions exceed max_expanded_nodes")
    _validate_node_provenance(plan.nodes, expected)


def _validate_expansion_identity(
    plan: PreflightExecutionPlan, group_id: str, template: CompiledForeach | None
) -> CompiledForeach:
    state = plan.expansions[group_id]
    if (
        template is None
        or state.group_id != group_id
        or state.template_digest != template.template_digest
        or state.workflow_signature != plan.workflow_signature
    ):
        raise ValueError(
            "Expansion does not match its frozen template and root signature"
        )
    return template


def _validate_expansion_evidence(
    template: CompiledForeach, state: ExpansionState
) -> None:
    root = expansion_directory(state.group_id)
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
    if state.digest != expansion_digest(
        template,
        state.workflow_signature,
        state.collection,
        state.items,
        state.caller_artifacts,
    ):
        raise ValueError("Expansion digest does not match its evidence")


def _reconstruct_expansion(
    template: CompiledForeach, state: ExpansionState
) -> ExpansionBatch:
    original = list(template.original_dependencies)
    if isinstance(template.items, str):
        source, _ = collection_reference(template.items)
        if source not in original:
            original.append(source)
    return concrete_batch(template, state, original)


def _validate_generated_nodes(
    nodes: dict[str, PreflightExecutionNode], batch: ExpansionBatch
) -> None:
    group = nodes[batch.state.group_id]
    if set(group.dependencies) != set(batch.group_dependencies):
        raise ValueError("Foreach completion gate differs from its instance terminals")
    for node in batch.nodes:
        if nodes.get(node.id) != node:
            raise ValueError(
                f"Generated node '{node.id}' differs from its frozen template"
            )


def _validate_node_provenance(
    nodes: list[PreflightExecutionNode], expected: dict[str, ExpansionProvenance | None]
) -> None:
    for node in nodes:
        if node.expansion != expected.get(node.id):
            raise ValueError(f"Node '{node.id}' has invalid expansion provenance")
        if node.input_item_slot:
            raise ValueError(
                "Uninstantiated item slots cannot enter an executable graph"
            )


def validate_branch_targets(plan: PreflightExecutionPlan) -> None:
    """Reject branch names shared by distinct logical worktrees in node order.

    Requires valid workspace policies. Explicit names override generated names;
    repeated targets for the same logical worktree are allowed. Inputs are
    unchanged and no branches are created.

    Raises:
        ValueError: The first expansion branch target collision.
    """
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
