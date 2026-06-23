from __future__ import annotations

from pathlib import Path

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

FIXTURES = Path(__file__).with_name("fixtures")


def provider(name: str, role: str = "executor") -> ProviderSpec:
    return ProviderSpec(provider=name, role=role)


def test_transitive_chain_matches_exact_golden() -> None:
    workflow = WorkflowPlan(
        name="transitive.chain",
        nodes=[
            WorkflowNode(
                id="design.init",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="a")],
                providers=[provider("copilot")],
            ),
            WorkflowNode(
                id="design.iteration",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="b")],
                needs=["design.init"],
                providers=[
                    provider("codex"),
                    provider("copilot", role="reviewer"),
                ],
            ),
            WorkflowNode(
                id="design.decision",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="c")],
                needs=["design.init", "design.iteration"],
                providers=[provider("copilot")],
            ),
        ],
    )
    state = build_initial_state(
        topology_from_workflow(workflow), run_id="run-transitive-chain"
    )
    layout = compute_topology_layout(topology_from_workflow(workflow))

    rendered = "\n".join(
        render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="design.iteration",
            width=120,
        )
    )
    expected = (
        (FIXTURES / "dag_render" / "transitive_chain" / "expected.txt")
        .read_text(encoding="utf-8")
        .rstrip("\n")
    )

    assert rendered == expected
