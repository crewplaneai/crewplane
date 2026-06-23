from __future__ import annotations

from pathlib import Path

from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.dag_render import render_dag_summary
from crewplane.observability.events import build_initial_state
from crewplane.observability.layout import compute_topology_layout
from tests.helpers.observability import topology_from_workflow
from tests.helpers.render_fixtures import read_render_fixture

FIXTURES = Path(__file__).parents[1] / "fixtures" / "dag_render"


def provider(
    name: str = "codex", role: ProviderRole = ProviderRole.EXECUTOR
) -> ProviderSpec:
    return ProviderSpec(provider=name, role=role)


def test_transitive_shortcut_dependencies_do_not_leave_stale_lanes() -> None:
    workflow = WorkflowPlan(
        name="transitive-shortcut-fanin-chain",
        nodes=[
            WorkflowNode(
                id="source.input",
                mode="input",
                source="{{file:source.md}}",
            ),
            WorkflowNode(
                id="branch.alpha",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                ],
                needs=["source.input"],
                providers=[provider()],
            ),
            WorkflowNode(
                id="branch.beta",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="b")
                ],
                needs=["source.input"],
                providers=[provider()],
            ),
            WorkflowNode(
                id="branch.gamma",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="c")
                ],
                needs=["source.input"],
                providers=[provider()],
            ),
            WorkflowNode(
                id="branch.delta",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="d")
                ],
                needs=["source.input"],
                providers=[provider()],
            ),
            WorkflowNode(
                id="branch.merge",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="e")
                ],
                needs=[
                    "branch.alpha",
                    "branch.beta",
                    "branch.gamma",
                    "branch.delta",
                ],
                providers=[provider()],
            ),
            WorkflowNode(
                id="apply.fix",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="f")
                ],
                needs=["source.input", "branch.merge"],
                providers=[provider()],
            ),
            WorkflowNode(
                id="verify.final",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="g")
                ],
                needs=["source.input", "branch.merge", "apply.fix"],
                providers=[provider()],
            ),
            WorkflowNode(
                id="handoff.done",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="h")
                ],
                needs=["branch.merge", "apply.fix", "verify.final"],
                providers=[provider()],
            ),
        ],
    )
    topology = topology_from_workflow(workflow)
    state = build_initial_state(
        topology,
        run_id="run-transitive-shortcut-fanin-chain",
    )
    layout = compute_topology_layout(topology)

    rendered = "\n".join(
        render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="apply.fix",
            width=140,
        )
    )

    assert rendered == read_render_fixture(
        FIXTURES,
        "transitive_shortcut_fanin_chain",
        "expected.txt",
    )
    assert "├─┬─┘ │" not in rendered
    assert "├─┼─┘" not in rendered
