"""Validate persisted preflight dependency graph contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from crewplane.core.workflow.keywords import ALLOWED_NODE_ARTIFACT_NAME_SET

if TYPE_CHECKING:
    from .models import (
        DependencyEdge,
        PreflightExecutionNode,
        PreflightExecutionPlan,
        RenderPlan,
    )


def validate_dependency_graph(
    plan: PreflightExecutionPlan,
    nodes_by_id: dict[str, PreflightExecutionNode],
) -> None:
    node_order = {node_id: index for index, node_id in enumerate(plan.execution_order)}
    graph_dependencies = {node_id: set[str]() for node_id in nodes_by_id}
    seen_edges: set[tuple[str, str, str | None]] = set()
    for edge in plan.dependency_graph:
        _validate_dependency_edge(edge, nodes_by_id, node_order)
        edge_key = (edge.source_node, edge.target_node, edge.artifact_name)
        if edge_key in seen_edges:
            raise ValueError(
                "Persisted dependency graph contains duplicate edge "
                f"'{edge.source_node}' -> '{edge.target_node}'."
            )
        seen_edges.add(edge_key)
        graph_dependencies[edge.target_node].add(edge.source_node)

    _validate_runtime_locator_dependencies(plan, seen_edges)

    for node in plan.nodes:
        node_dependencies = set(node.dependencies)
        if graph_dependencies[node.id] != node_dependencies:
            raise ValueError(
                f"Persisted node '{node.id}' dependencies disagree with the "
                "compiled dependency graph."
            )


def _validate_runtime_locator_dependencies(
    plan: PreflightExecutionPlan,
    dependency_edges: set[tuple[str, str, str | None]],
) -> None:
    for render_plan in plan.render_plans:
        _validate_render_plan_runtime_locator_dependencies(
            render_plan,
            dependency_edges,
        )


def _validate_render_plan_runtime_locator_dependencies(
    render_plan: RenderPlan,
    dependency_edges: set[tuple[str, str, str | None]],
) -> None:
    if render_plan.node_id is None:
        return
    for stream in render_plan.streams:
        for fragment in stream.fragments:
            if fragment.kind != "runtime_locator_lookup":
                continue
            locator = fragment.locator
            if locator is None:
                continue
            dependency_edge = (
                locator["node_id"],
                render_plan.node_id,
                locator["artifact_name"],
            )
            if dependency_edge not in dependency_edges:
                raise ValueError(
                    f"Persisted render plan '{render_plan.render_plan_id}' "
                    f"contains runtime locator '{locator['node_id']}."
                    f"{locator['artifact_name']}' without a matching dependency "
                    "edge."
                )


def _validate_dependency_edge(
    edge: DependencyEdge,
    nodes_by_id: dict[str, PreflightExecutionNode],
    node_order: dict[str, int],
) -> None:
    if not edge.source_node.strip() or not edge.target_node.strip():
        raise ValueError("Persisted dependency graph node ids cannot be blank.")
    if edge.source_node not in nodes_by_id:
        raise ValueError(
            "Persisted dependency graph references unknown source node "
            f"'{edge.source_node}'."
        )
    if edge.target_node not in nodes_by_id:
        raise ValueError(
            "Persisted dependency graph references unknown target node "
            f"'{edge.target_node}'."
        )
    if node_order[edge.source_node] >= node_order[edge.target_node]:
        raise ValueError(
            "Persisted dependency graph edge must point from an upstream node: "
            f"'{edge.source_node}' -> '{edge.target_node}'."
        )
    if edge.artifact_name not in {None, *ALLOWED_NODE_ARTIFACT_NAME_SET}:
        raise ValueError(
            f"Persisted dependency graph contains unknown artifact "
            f"'{edge.artifact_name}'."
        )
    if (
        edge.artifact_name is not None
        and edge.artifact_name.startswith("findings")
        and not nodes_by_id[edge.source_node].findings
    ):
        raise ValueError(
            f"Persisted dependency graph requests findings from node "
            f"'{edge.source_node}', which does not publish findings."
        )
    expected_artifact_key = edge.artifact_name
    expected_target_locator = (
        edge.source_node
        if edge.artifact_name is None
        else f"{edge.source_node}.{edge.artifact_name}"
    )
    if edge.artifact_key != expected_artifact_key:
        raise ValueError("Persisted dependency graph artifact_key is inconsistent.")
    if edge.target_locator != expected_target_locator:
        raise ValueError("Persisted dependency graph target_locator is inconsistent.")
