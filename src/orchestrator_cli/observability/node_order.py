from __future__ import annotations

from orchestrator_cli.observability.events import RunDashboardState
from orchestrator_cli.observability.layout import TopologyLayout


def topological_node_order(
    state: RunDashboardState,
    layout: TopologyLayout,
) -> list[str]:
    """Return dashboard node ids in layout wave order, followed by state extras."""

    ordered_node_ids: list[str] = []
    seen: set[str] = set()
    for wave in layout.waves:
        for node_id in wave:
            if node_id in state.nodes and node_id not in seen:
                seen.add(node_id)
                ordered_node_ids.append(node_id)

    if len(ordered_node_ids) == len(state.nodes):
        return ordered_node_ids

    extras = sorted(
        (node_id for node_id in state.nodes if node_id not in seen),
        key=state.node_order.__getitem__,
    )
    ordered_node_ids.extend(extras)
    return ordered_node_ids
