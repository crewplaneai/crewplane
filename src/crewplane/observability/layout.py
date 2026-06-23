from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from crewplane.observability.types import WorkflowTopology


@dataclass(frozen=True)
class NodePlacement:
    """Column and wave placement for a workflow node in a topology view."""

    node_id: str
    wave_index: int
    lane_start: int
    lane_end: int


@dataclass(frozen=True)
class TopologyLayout:
    """Computed DAG layout and dependency maps for dashboard renderers."""

    waves: tuple[tuple[str, ...], ...]
    placements: Mapping[str, NodePlacement]
    lane_count: int
    node_order: Mapping[str, int]
    dependencies: Mapping[str, tuple[str, ...]]
    dependents: Mapping[str, tuple[str, ...]]

    def __deepcopy__(self, memo: dict[int, object]) -> TopologyLayout:
        memo[id(self)] = self
        return self


def compute_topology_layout(topology: WorkflowTopology) -> TopologyLayout:
    """Compute stable wave and lane placement for a workflow DAG."""

    waves = _topological_waves(topology)
    node_order = dict(topology.node_order)
    nodes_by_id = {node.id: node for node in topology.nodes}
    dependencies, dependents = _build_dependency_maps(topology)

    placements: dict[str, NodePlacement] = {}
    lane_count = 0

    for wave_index, wave_nodes in enumerate(waves):
        occupied: list[tuple[int, int]] = []
        ordered_wave_nodes = sorted(wave_nodes, key=node_order.__getitem__)
        for node_id in ordered_wave_nodes:
            node = nodes_by_id[node_id]
            lane_start, lane_end = _base_lane_span(
                list(node.dependencies),
                placements,
                lane_count,
            )
            lane_start, lane_end = _shift_for_collisions(
                lane_start=lane_start,
                lane_end=lane_end,
                occupied=occupied,
            )
            occupied.append((lane_start, lane_end))
            placements[node_id] = NodePlacement(
                node_id=node_id,
                wave_index=wave_index,
                lane_start=lane_start,
                lane_end=lane_end,
            )
            lane_count = max(lane_count, lane_end + 1)

    return TopologyLayout(
        waves=tuple(tuple(wave) for wave in waves),
        placements=MappingProxyType(dict(placements)),
        lane_count=lane_count,
        node_order=MappingProxyType(dict(node_order)),
        dependencies=MappingProxyType(
            {
                node_id: tuple(sorted(needs, key=node_order.__getitem__))
                for node_id, needs in dependencies.items()
            }
        ),
        dependents=MappingProxyType(
            {
                node_id: tuple(sorted(children, key=node_order.__getitem__))
                for node_id, children in dependents.items()
            }
        ),
    )


def _build_dependency_maps(
    topology: WorkflowTopology,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    dependencies: dict[str, set[str]] = {
        node.id: set(node.dependencies) for node in topology.nodes
    }
    dependents: dict[str, set[str]] = {node.id: set() for node in topology.nodes}
    for node in topology.nodes:
        for dependency_id in node.dependencies:
            if dependency_id not in dependents:
                raise ValueError(
                    f"Node '{node.id}' depends on unknown node '{dependency_id}'."
                )
            dependents[dependency_id].add(node.id)
    return dependencies, dependents


def _topological_waves(topology: WorkflowTopology) -> list[list[str]]:
    dependencies, dependents = _build_dependency_maps(topology)
    remaining = {
        node_id: len(node_dependencies)
        for node_id, node_dependencies in dependencies.items()
    }
    node_order = dict(topology.node_order)
    ready = sorted(
        (
            node_id
            for node_id, dependency_count in remaining.items()
            if dependency_count == 0
        ),
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

    if visited != len(topology.nodes):
        raise ValueError("Workflow graph contains a cycle.")
    return waves


def _base_lane_span(
    dependencies: list[str],
    placements: dict[str, NodePlacement],
    lane_count: int,
) -> tuple[int, int]:
    if not dependencies:
        return lane_count, lane_count

    parent_spans: list[tuple[int, int]] = []
    for dependency in dependencies:
        placement = placements.get(dependency)
        if placement is None:
            raise ValueError(
                "Topology layout requires upstream node placements to exist: "
                f"missing '{dependency}'."
            )
        parent_spans.append((placement.lane_start, placement.lane_end))

    lane_start = min(start for start, _ in parent_spans)
    lane_end = max(end for _, end in parent_spans)
    return lane_start, lane_end


def _shift_for_collisions(
    lane_start: int,
    lane_end: int,
    occupied: list[tuple[int, int]],
) -> tuple[int, int]:
    shifted_start = lane_start
    shifted_end = lane_end
    while _overlaps_any(shifted_start, shifted_end, occupied):
        shifted_start += 1
        shifted_end += 1
    return shifted_start, shifted_end


def _overlaps_any(
    lane_start: int,
    lane_end: int,
    occupied: list[tuple[int, int]],
) -> bool:
    for existing_start, existing_end in occupied:
        if lane_start > existing_end or lane_end < existing_start:
            continue
        return True
    return False
