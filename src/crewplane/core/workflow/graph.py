from dataclasses import dataclass

from .models import WorkflowPlan


@dataclass(frozen=True)
class WorkflowGraphAnalysis:
    """Topological waves and transitive ancestors for a valid workflow DAG."""

    waves: tuple[tuple[str, ...], ...]
    ancestors: dict[str, frozenset[str]]


def build_dependency_maps(
    workflow: WorkflowPlan,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    dependencies: dict[str, set[str]] = {
        node.id: set(node.needs) for node in workflow.nodes
    }
    dependents: dict[str, set[str]] = {node.id: set() for node in workflow.nodes}
    for node in workflow.nodes:
        for needed in node.needs:
            if needed not in dependents:
                raise ValueError(
                    f"Node '{node.id}' depends on unknown node '{needed}'."
                )
            dependents[needed].add(node.id)
    return dependencies, dependents


def topological_waves(workflow: WorkflowPlan) -> list[list[str]]:
    return [list(wave) for wave in analyze_workflow_graph(workflow).waves]


def ancestor_map(workflow: WorkflowPlan) -> dict[str, set[str]]:
    return {
        node_id: set(ancestors)
        for node_id, ancestors in analyze_workflow_graph(workflow).ancestors.items()
    }


def analyze_workflow_graph(workflow: WorkflowPlan) -> WorkflowGraphAnalysis:
    """Analyze one workflow graph, raising on unknown nodes or cycles."""

    dependencies, dependents = build_dependency_maps(workflow)
    remaining: dict[str, int] = {
        node_id: len(needs) for node_id, needs in dependencies.items()
    }
    ancestors: dict[str, set[str]] = {node_id: set() for node_id in dependencies}
    node_order = {node.id: index for index, node in enumerate(workflow.nodes)}
    ready = sorted(
        (node_id for node_id, count in remaining.items() if count == 0),
        key=node_order.__getitem__,
    )
    waves: list[tuple[str, ...]] = []
    visited = 0

    while ready:
        current_wave = tuple(ready)
        waves.append(current_wave)
        next_ready: list[str] = []
        for node_id in current_wave:
            visited += 1
            for dependent_id in sorted(dependents[node_id], key=node_order.__getitem__):
                ancestors[dependent_id].update(ancestors[node_id])
                ancestors[dependent_id].add(node_id)
                remaining[dependent_id] -= 1
                if remaining[dependent_id] == 0:
                    next_ready.append(dependent_id)
        ready = sorted(next_ready, key=node_order.__getitem__)

    if visited != len(workflow.nodes):
        raise ValueError("Workflow graph contains a cycle.")
    return WorkflowGraphAnalysis(
        waves=tuple(waves),
        ancestors={
            node_id: frozenset(node_ancestors)
            for node_id, node_ancestors in ancestors.items()
        },
    )
