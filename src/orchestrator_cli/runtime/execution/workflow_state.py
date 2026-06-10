from __future__ import annotations

from orchestrator_cli.core.preflight.models import (
    DependencyEdge,
    PreflightExecutionPlan,
)

from .common import WorkflowExecutionState


def initialize_workflow_execution_state(
    plan: PreflightExecutionPlan,
    resumed_node_ids: tuple[str, ...] = (),
) -> WorkflowExecutionState:
    node_order = {node_id: index for index, node_id in enumerate(plan.execution_order)}
    for node in plan.nodes:
        node_order.setdefault(node.id, len(node_order))
    dependencies_by_node = dependencies_by_node_from_plan(plan)
    resumed_nodes = validated_resumed_nodes(resumed_node_ids, node_order)
    remaining_dependencies = {
        node_id: len(dependencies)
        for node_id, dependencies in dependencies_by_node.items()
    }
    dependents = build_dependents_map(dependencies_by_node)
    for node_id in sorted(resumed_nodes, key=node_order.__getitem__):
        for dependent_id in dependents[node_id]:
            remaining_dependencies[dependent_id] -= 1
    ready = sorted(
        (
            node_id
            for node_id, dependency_count in remaining_dependencies.items()
            if dependency_count == 0 and node_id not in resumed_nodes
        ),
        key=node_order.__getitem__,
    )
    return WorkflowExecutionState(
        ready=ready,
        running={},
        statuses={
            node.id: ("succeeded" if node.id in resumed_nodes else "pending")
            for node in plan.nodes
        },
        node_errors={},
        failed_dependencies={node.id: set() for node in plan.nodes},
        remaining_dependencies=remaining_dependencies,
        dependents=dependents,
        dependencies_by_node=dependencies_by_node,
        node_order=node_order,
    )


def dependencies_by_node_from_plan(
    plan: PreflightExecutionPlan,
) -> dict[str, set[str]]:
    node_ids = {node.id for node in plan.nodes}
    dependencies = {node.id: set() for node in plan.nodes}
    for edge in plan.dependency_graph:
        validate_dependency_edge(edge, node_ids)
        dependencies[edge.target_node].add(edge.source_node)
    return dependencies


def validate_dependency_edge(edge: DependencyEdge, node_ids: set[str]) -> None:
    if edge.source_node not in node_ids:
        raise ValueError(
            "Compiled dependency graph references unknown source node "
            f"'{edge.source_node}'."
        )
    if edge.target_node not in node_ids:
        raise ValueError(
            "Compiled dependency graph references unknown target node "
            f"'{edge.target_node}'."
        )


def build_dependents_map(
    dependencies: dict[str, set[str]],
) -> dict[str, list[str]]:
    dependents: dict[str, list[str]] = {node_id: [] for node_id in dependencies}
    for node_id, node_dependencies in dependencies.items():
        for needed in node_dependencies:
            dependents[needed].append(node_id)
    return dependents


def validated_resumed_nodes(
    resumed_node_ids: tuple[str, ...],
    node_order: dict[str, int],
) -> set[str]:
    resumed_nodes = set(resumed_node_ids)
    unknown_nodes = sorted(resumed_nodes - set(node_order))
    if unknown_nodes:
        raise ValueError(f"Resumed node ids are not in the plan: {unknown_nodes}")
    return resumed_nodes
