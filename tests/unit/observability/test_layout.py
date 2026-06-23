import unittest

from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.layout import compute_topology_layout
from tests.helpers.observability import topology_from_workflow


def provider(name: str) -> ProviderSpec:
    return ProviderSpec(provider=name)


class TopologyLayoutTests(unittest.TestCase):
    def test_parallel_plus_fanin_spans_parent_lanes(self) -> None:
        workflow = WorkflowPlan(
            name="fanin.example",
            nodes=[
                WorkflowNode(
                    id="node1",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                    ],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="node2",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="b")
                    ],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="node3",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="c")
                    ],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="node5",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="merge")
                    ],
                    needs=["node2", "node3"],
                    providers=[ProviderSpec(provider="p", role=ProviderRole.EXECUTOR)],
                ),
            ],
        )

        layout = compute_topology_layout(topology_from_workflow(workflow))
        self.assertEqual(layout.placements["node1"].lane_start, 0)
        self.assertEqual(layout.placements["node2"].lane_start, 1)
        self.assertEqual(layout.placements["node3"].lane_start, 2)
        self.assertEqual(layout.placements["node5"].lane_start, 1)
        self.assertEqual(layout.placements["node5"].lane_end, 2)

    def test_diamond_layout_assigns_deterministic_shift(self) -> None:
        workflow = WorkflowPlan(
            name="diamond",
            nodes=[
                WorkflowNode(
                    id="root",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="root")
                    ],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="left",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="left")
                    ],
                    needs=["root"],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="right",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="right")
                    ],
                    needs=["root"],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="merge",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="merge")
                    ],
                    needs=["left", "right"],
                    providers=[ProviderSpec(provider="p", role=ProviderRole.EXECUTOR)],
                ),
            ],
        )

        layout = compute_topology_layout(topology_from_workflow(workflow))
        self.assertEqual(layout.placements["root"].lane_start, 0)
        self.assertEqual(layout.placements["left"].lane_start, 0)
        self.assertEqual(layout.placements["right"].lane_start, 1)
        self.assertEqual(layout.placements["merge"].lane_start, 0)
        self.assertEqual(layout.placements["merge"].lane_end, 1)

    def test_fan_out_then_fan_in_spans_full_parent_range(self) -> None:
        workflow = WorkflowPlan(
            name="fanout-fanin",
            nodes=[
                WorkflowNode(
                    id="root",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="root")
                    ],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="child.a",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                    ],
                    needs=["root"],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="child.b",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="b")
                    ],
                    needs=["root"],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="child.c",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="c")
                    ],
                    needs=["root"],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="merge.all",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="merge")
                    ],
                    needs=["child.a", "child.b", "child.c"],
                    providers=[ProviderSpec(provider="p", role=ProviderRole.EXECUTOR)],
                ),
            ],
        )

        layout = compute_topology_layout(topology_from_workflow(workflow))
        self.assertEqual(layout.placements["child.a"].lane_start, 0)
        self.assertEqual(layout.placements["child.b"].lane_start, 1)
        self.assertEqual(layout.placements["child.c"].lane_start, 2)
        self.assertEqual(layout.placements["merge.all"].lane_start, 0)
        self.assertEqual(layout.placements["merge.all"].lane_end, 2)

    def test_non_contiguous_parent_lanes_expand_span(self) -> None:
        workflow = WorkflowPlan(
            name="non-contiguous",
            nodes=[
                WorkflowNode(
                    id="root.1",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="1")
                    ],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="root.2",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="2")
                    ],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="root.3",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="3")
                    ],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="merge.1.3",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="merge")
                    ],
                    needs=["root.1", "root.3"],
                    providers=[ProviderSpec(provider="p", role=ProviderRole.EXECUTOR)],
                ),
            ],
        )

        layout = compute_topology_layout(topology_from_workflow(workflow))
        self.assertEqual(layout.placements["root.1"].lane_start, 0)
        self.assertEqual(layout.placements["root.2"].lane_start, 1)
        self.assertEqual(layout.placements["root.3"].lane_start, 2)
        self.assertEqual(layout.placements["merge.1.3"].lane_start, 0)
        self.assertEqual(layout.placements["merge.1.3"].lane_end, 2)

    def test_disconnected_components_keep_stable_lanes(self) -> None:
        workflow = WorkflowPlan(
            name="disconnected",
            nodes=[
                WorkflowNode(
                    id="a",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                    ],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="b",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="b")
                    ],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="a.done",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="done a")
                    ],
                    needs=["a"],
                    providers=[ProviderSpec(provider="p", role=ProviderRole.EXECUTOR)],
                ),
                WorkflowNode(
                    id="b.done",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="done b")
                    ],
                    needs=["b"],
                    providers=[ProviderSpec(provider="p", role=ProviderRole.EXECUTOR)],
                ),
            ],
        )

        layout = compute_topology_layout(topology_from_workflow(workflow))
        self.assertEqual(layout.placements["a"].lane_start, 0)
        self.assertEqual(layout.placements["b"].lane_start, 1)
        self.assertEqual(layout.placements["a.done"].lane_start, 0)
        self.assertEqual(layout.placements["b.done"].lane_start, 1)

    def test_import_prefixed_node_ids_keep_deterministic_layout(self) -> None:
        workflow = WorkflowPlan(
            name="imports-layout",
            nodes=[
                WorkflowNode(
                    id="auth.plan",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="plan")
                    ],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="auth.review",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="review")
                    ],
                    needs=["auth.plan"],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="summary.final",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="summary")
                    ],
                    needs=["auth.review"],
                    providers=[ProviderSpec(provider="p", role=ProviderRole.EXECUTOR)],
                ),
            ],
        )

        layout = compute_topology_layout(topology_from_workflow(workflow))
        self.assertEqual(
            layout.waves, (("auth.plan",), ("auth.review",), ("summary.final",))
        )
        self.assertEqual(layout.placements["auth.plan"].lane_start, 0)
        self.assertEqual(layout.placements["auth.review"].lane_start, 0)
        self.assertEqual(layout.placements["summary.final"].lane_start, 0)
