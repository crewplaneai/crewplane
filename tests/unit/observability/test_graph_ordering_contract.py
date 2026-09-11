from __future__ import annotations

from copy import deepcopy

import pytest

from crewplane.architecture.contracts import TopologyNode, WorkflowTopology
from crewplane.core.workflow.graph import ancestor_map, topological_waves
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan
from crewplane.observability.layout import compute_topology_layout


@pytest.mark.parametrize(
    ("nodes", "waves", "ancestors"),
    [
        ([], [], {}),
        ([("z", []), ("a", [])], [["z", "a"]], {"z": set(), "a": set()}),
        (
            [("z", []), ("a", ["z", "z"]), ("b", ["z"]), ("c", ["b", "a"])],
            [["z"], ["a", "b"], ["c"]],
            {"z": set(), "a": {"z"}, "b": {"z"}, "c": {"z", "a", "b"}},
        ),
        (
            [("b", ["a"]), ("z", []), ("a", [])],
            [["z", "a"], ["b"]],
            {"b": {"a"}, "z": set(), "a": set()},
        ),
    ],
)
def test_authored_and_observer_graph_order_and_ancestors(nodes, waves, ancestors):
    workflow, topology = _graph_inputs(nodes)
    assert topological_waves(workflow) == waves
    assert ancestor_map(workflow) == ancestors
    layout = compute_topology_layout(topology)
    assert layout.waves == tuple(tuple(wave) for wave in waves)
    assert dict(layout.dependencies) == {
        node_id: tuple(sorted(set(needs), key=topology.node_order.__getitem__))
        for node_id, needs in nodes
    }
    assert deepcopy(layout) is layout
    with pytest.raises(TypeError):
        layout.placements["new"] = None


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ([("a", []), ("a", [])], "Workflow graph contains a cycle."),
        ([("a", ["a"])], "Workflow graph contains a cycle."),
        ([("a", ["b"]), ("b", ["a"])], "Workflow graph contains a cycle."),
        (
            [("a", ["missing-z", "missing-a"]), ("b", ["missing-b"])],
            "Node 'a' depends on unknown node 'missing-z'.",
        ),
        (
            [("a", ["missing"]), ("a", [])],
            "Node 'a' depends on unknown node 'missing'.",
        ),
        (
            [("a", ["a"]), ("b", ["missing"])],
            "Node 'b' depends on unknown node 'missing'.",
        ),
    ],
)
def test_authored_and_observer_graph_errors_preserve_precedence(nodes, message):
    workflow, topology = _graph_inputs(nodes)
    with pytest.raises(ValueError) as authored:
        topological_waves(workflow)
    with pytest.raises(ValueError) as observer:
        compute_topology_layout(topology)
    assert str(authored.value) == str(observer.value) == message


def _graph_inputs(nodes):
    workflow = WorkflowPlan(
        name="graph",
        nodes=[
            WorkflowNode(id=node_id, mode="parallel", needs=needs)
            for node_id, needs in nodes
        ],
    )
    topology = WorkflowTopology(
        "graph",
        tuple(
            TopologyNode(node_id, "parallel", tuple(needs)) for node_id, needs in nodes
        ),
    )
    return workflow, topology
