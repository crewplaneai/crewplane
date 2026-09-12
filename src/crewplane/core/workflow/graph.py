from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass

from .models import WorkflowPlan


@dataclass(frozen=True)
class WorkflowGraphAnalysis:
    """Topological waves and transitive ancestors for a valid workflow DAG."""

    waves: tuple[tuple[str, ...], ...]
    ancestors: dict[str, frozenset[str]]


def has_valid_dependency_references(workflow: WorkflowPlan) -> bool:
    """Check graph prerequisites without classifying cycles."""

    node_ids = {node.id for node in workflow.nodes}
    if len(node_ids) != len(workflow.nodes):
        return False
    dependencies = {node.id: set(node.needs) for node in workflow.nodes}
    if any(node_id in needs for node_id, needs in dependencies.items()):
        return False
    return all(needs <= node_ids for needs in dependencies.values())


def build_dependency_maps(
    workflow: WorkflowPlan,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    return build_graph_dependency_maps(
        [(node.id, node.needs) for node in workflow.nodes]
    )


def build_graph_dependency_maps(
    nodes: Sequence[tuple[str, Sequence[str]]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    dependencies = {node_id: set(needs) for node_id, needs in nodes}
    dependents: dict[str, set[str]] = {node_id: set() for node_id, _ in nodes}
    for node_id, needs in nodes:
        for needed in needs:
            if needed not in dependents:
                raise ValueError(
                    f"Node '{node_id}' depends on unknown node '{needed}'."
                )
            dependents[needed].add(node_id)
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
    waves = ordered_graph_waves(
        [node.id for node in workflow.nodes], dependencies, dependents
    )
    ancestors: dict[str, set[str]] = {node_id: set() for node_id in dependencies}
    for wave in waves:
        for node_id in wave:
            for needed in dependencies[node_id]:
                ancestors[node_id].update(ancestors[needed])
                ancestors[node_id].add(needed)
    return WorkflowGraphAnalysis(
        waves=waves,
        ancestors={
            node_id: frozenset(node_ancestors)
            for node_id, node_ancestors in ancestors.items()
        },
    )


def ordered_graph_waves(
    node_ids: Sequence[str],
    dependencies: Mapping[str, Set[str]],
    dependents: Mapping[str, Set[str]],
) -> tuple[tuple[str, ...], ...]:
    remaining = {node_id: len(needs) for node_id, needs in dependencies.items()}
    node_order = {node_id: index for index, node_id in enumerate(node_ids)}
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
                remaining[dependent_id] -= 1
                if remaining[dependent_id] == 0:
                    next_ready.append(dependent_id)
        ready = sorted(next_ready, key=node_order.__getitem__)

    if visited != len(node_ids):
        raise ValueError("Workflow graph contains a cycle.")
    return tuple(waves)
