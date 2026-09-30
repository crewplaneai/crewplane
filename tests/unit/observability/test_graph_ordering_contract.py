from __future__ import annotations

from copy import deepcopy

import pytest

from crewplane.architecture.contracts import (
    ArtifactContract,
    TopologyNode,
    WorkflowTopology,
)
from crewplane.cli.dry_run import preview_topological_waves
from crewplane.core.preflight import PreflightCompilationPreview, PreflightExecutionNode
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
    assert preview_topological_waves(_preview(nodes)) == waves
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


@pytest.mark.parametrize(
    ("nodes", "order", "waves"),
    [
        ([], ["extra"], []),
        ([("c", ["b"]), ("b", ["a"]), ("a", [])], [], [["a"], ["b"], ["c"]]),
        ([("z", []), ("a", []), ("b", [])], ["a"], [["a", "z", "b"]]),
        ([("z", []), ("a", [])], ["extra", "a"], [["a", "z"]]),
        ([("z", []), ("a", [])], ["a", "a", "a"], [["z", "a"]]),
        (
            [("z", []), ("a", ["z", "z"]), ("b", ["z"]), ("c", ["a", "b"])],
            ["c", "b", "a", "z"],
            [["z"], ["b", "a"], ["c"]],
        ),
        ([("a", ["missing"]), ("b", []), ("a", [])], [], [["a", "b"]]),
        ([("a", []), ("b", []), ("a", ["b"])], [], [["b"], ["a"]]),
    ],
)
def test_preview_preserves_ordering_and_duplicate_node_collapse(nodes, order, waves):
    assert preview_topological_waves(_preview(nodes, order)) == waves


@pytest.mark.parametrize(
    "nodes",
    [
        [("a", ["a"])],
        [("a", ["b"]), ("b", ["a"])],
        [("a", ["missing"])],
        [("a", []), ("a", ["missing"])],
    ],
)
def test_preview_preserves_cycle_diagnostic_for_invalid_graphs(nodes):
    with pytest.raises(ValueError) as error:
        preview_topological_waves(_preview(nodes))
    assert str(error.value) == "Compiled preview dependency graph contains a cycle."


def _preview(nodes, order=None):
    return PreflightCompilationPreview(
        execution_order=[] if order is None else order,
        nodes=[
            PreflightExecutionNode(
                id=node_id,
                mode="parallel",
                dependencies=needs,
                artifact_contract=ArtifactContract(output_path=f"{node_id}-result.md"),
            )
            for node_id, needs in nodes
        ],
    )


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
