from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from crewplane.architecture.contracts import WorkflowTopology
from crewplane.core.workflow.graph import (
    build_graph_dependency_maps,
    ordered_graph_waves,
)


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

    dependencies, dependents = build_graph_dependency_maps(
        [(node.id, node.dependencies) for node in topology.nodes]
    )
    waves = ordered_graph_waves(
        [node.id for node in topology.nodes], dependencies, dependents
    )
    node_order = dict(topology.node_order)
    nodes_by_id = {node.id: node for node in topology.nodes}

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
