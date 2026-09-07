from pathlib import Path

import pytest

from crewplane.architecture.contracts import (
    TopologyNode,
    TopologyProvider,
    WorkflowTopology,
)
from crewplane.observability.dag_graph import build_graph_states
from crewplane.observability.dag_render import render_dag_summary
from crewplane.observability.events import build_initial_state
from crewplane.observability.layout import compute_topology_layout
from crewplane.observability.node_order import topological_node_order
from crewplane.observability.text_layout import display_width
from tests.helpers.render_fixtures import read_render_fixture

FIXTURES = Path(__file__).parents[1] / "fixtures" / "dag_render"


@pytest.fixture
def nested_fanout_topology() -> WorkflowTopology:
    dependencies = {
        "source.input": (),
        "stage.prepare": ("source.input",),
        "branch.alpha": ("source.input", "stage.prepare"),
        "branch.beta.with.long.name": ("source.input", "stage.prepare"),
        "branch.gamma": ("source.input", "stage.prepare"),
        "branch.alpha.continuation": (
            "source.input",
            "stage.prepare",
            "branch.alpha",
        ),
        "merge.first": (
            "source.input",
            "stage.prepare",
            "branch.alpha.continuation",
            "branch.beta.with.long.name",
            "branch.gamma",
        ),
        "branch.delta": (
            "source.input",
            "stage.prepare",
            "merge.first",
        ),
        "branch.epsilon": (
            "source.input",
            "stage.prepare",
            "merge.first",
        ),
        "merge.second": (
            "source.input",
            "stage.prepare",
            "merge.first",
            "branch.delta",
            "branch.epsilon",
        ),
        "stage.middle": (
            "source.input",
            "stage.prepare",
            "branch.alpha.continuation",
            "branch.beta.with.long.name",
            "branch.gamma",
            "merge.second",
        ),
        "branch.zeta": (
            "source.input",
            "stage.prepare",
            "branch.alpha.continuation",
            "branch.beta.with.long.name",
            "branch.gamma",
            "merge.second",
            "stage.middle",
        ),
        "branch.eta.with.extended.label": (
            "source.input",
            "stage.prepare",
            "merge.second",
            "stage.middle",
        ),
        "merge.third": (
            "source.input",
            "stage.prepare",
            "branch.alpha.continuation",
            "branch.beta.with.long.name",
            "branch.gamma",
            "merge.second",
            "stage.middle",
            "branch.zeta",
            "branch.eta.with.extended.label",
        ),
        "stage.finish": (
            "source.input",
            "stage.prepare",
            "branch.alpha.continuation",
            "branch.beta.with.long.name",
            "branch.gamma",
            "merge.second",
            "stage.middle",
            "branch.zeta",
            "branch.eta.with.extended.label",
            "merge.third",
        ),
        "output.final": ("merge.third", "stage.finish"),
    }
    return WorkflowTopology(
        workflow_name="nested-fanout-with-transitive-dependencies",
        nodes=tuple(
            TopologyNode(
                id=node_id,
                mode="input" if node_id == "source.input" else "parallel",
                dependencies=needs,
                providers=()
                if node_id == "source.input"
                else (TopologyProvider(provider="provider-a"),),
            )
            for node_id, needs in dependencies.items()
        ),
    )


def test_nested_fanout_keeps_active_dependencies_in_their_columns(
    nested_fanout_topology: WorkflowTopology,
) -> None:
    state = build_initial_state(nested_fanout_topology, run_id="nested-fanout")
    layout = compute_topology_layout(nested_fanout_topology)

    for graph_state in build_graph_states(
        topological_node_order(state, layout), layout
    ):
        for column, target in enumerate(graph_state.before_columns):
            if target is not None and target != graph_state.node_id:
                assert graph_state.after_columns[column] == target, graph_state.node_id


def test_transitive_fanout_matches_golden(
    nested_fanout_topology: WorkflowTopology,
) -> None:
    state = build_initial_state(nested_fanout_topology, run_id="nested-fanout")
    layout = compute_topology_layout(nested_fanout_topology)

    lines = render_dag_summary(state, layout, "branch.alpha", width=120)

    assert "\n".join(lines) == read_render_fixture(
        FIXTURES, "nested_fanout_with_transitive_dependencies", "expected.txt"
    )


@pytest.mark.parametrize("width", [60, 80, 120])
def test_long_node_names_keep_statuses_aligned(
    nested_fanout_topology: WorkflowTopology, width: int
) -> None:
    state = build_initial_state(nested_fanout_topology, run_id="nested-fanout")
    layout = compute_topology_layout(nested_fanout_topology)

    lines = render_dag_summary(state, layout, "branch.alpha", width=width)
    node_lines = [line for line in lines if "●" in line]

    assert len(node_lines) == len(nested_fanout_topology.nodes)
    assert len({display_width(line.split("⏸")[0]) for line in node_lines}) == 1
    assert all("⏸" in line for line in node_lines)
    assert all(display_width(line) <= width for line in lines)


def test_nested_fanout_beside_an_independent_branch_matches_golden() -> None:
    topology = WorkflowTopology(
        workflow_name="nested-fanout",
        nodes=(
            TopologyNode(id="root", mode="parallel"),
            TopologyNode(id="branch", mode="parallel", dependencies=("root",)),
            TopologyNode(id="sibling", mode="parallel", dependencies=("root",)),
            TopologyNode(id="left", mode="parallel", dependencies=("branch",)),
            TopologyNode(id="right", mode="parallel", dependencies=("branch",)),
        ),
    )
    state = build_initial_state(topology, run_id="nested-fanout")
    layout = compute_topology_layout(topology)

    lines = render_dag_summary(state, layout, "branch", width=120)

    assert "\n".join(lines) == read_render_fixture(
        FIXTURES, "nested_fanout_with_sibling", "expected.txt"
    )


@pytest.mark.parametrize("width", [20, 40, 60])
def test_oversized_node_names_leave_room_for_status(width: int) -> None:
    topology = WorkflowTopology(
        workflow_name="oversized-node-name",
        nodes=(
            TopologyNode(id="short", mode="parallel"),
            TopologyNode(id="very-long-name-" * 8, mode="parallel"),
        ),
    )
    state = build_initial_state(topology, run_id="oversized-node-name")
    layout = compute_topology_layout(topology)
    state.nodes["short"].status = "failed"

    lines = render_dag_summary(state, layout, None, width=width)

    assert "❌" in lines[0]
    assert "⏸" in lines[1]
    assert display_width(lines[0].split("❌")[0]) == display_width(
        lines[1].split("⏸")[0]
    )
    assert "..." in lines[1].split("⏸")[0]
    assert all(display_width(line) <= width for line in lines)
