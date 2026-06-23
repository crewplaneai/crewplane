from collections import deque

from .models import WorkflowPlan


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
    dependencies, dependents = build_dependency_maps(workflow)
    remaining: dict[str, int] = {
        node_id: len(needs) for node_id, needs in dependencies.items()
    }
    node_order = {node.id: index for index, node in enumerate(workflow.nodes)}
    ready = sorted(
        (node_id for node_id, count in remaining.items() if count == 0),
        key=node_order.__getitem__,
    )
    waves: list[list[str]] = []
    visited = 0

    while ready:
        current_wave = ready
        waves.append(current_wave)
        next_ready: list[str] = []
        for node_id in current_wave:
            visited += 1
            for dependent_id in sorted(dependents[node_id], key=node_order.__getitem__):
                remaining[dependent_id] -= 1
                if remaining[dependent_id] == 0:
                    next_ready.append(dependent_id)
        ready = sorted(next_ready, key=node_order.__getitem__)

    if visited != len(workflow.nodes):
        raise ValueError("Workflow graph contains a cycle.")
    return waves


def ancestor_map(workflow: WorkflowPlan) -> dict[str, set[str]]:
    dependencies, dependents = build_dependency_maps(workflow)
    ancestors: dict[str, set[str]] = {node_id: set() for node_id in dependencies}
    queue = deque(node_id for node_id, needs in dependencies.items() if not needs)
    indegree: dict[str, int] = {
        node_id: len(needs) for node_id, needs in dependencies.items()
    }
    visited = 0

    while queue:
        node_id = queue.popleft()
        visited += 1
        for dependent_id in sorted(dependents[node_id]):
            ancestors[dependent_id].update(ancestors[node_id])
            ancestors[dependent_id].add(node_id)
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                queue.append(dependent_id)

    if visited != len(workflow.nodes):
        raise ValueError("Workflow graph contains a cycle.")

    return ancestors
