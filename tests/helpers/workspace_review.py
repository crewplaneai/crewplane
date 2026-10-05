from __future__ import annotations

from pathlib import Path

from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from tests.helpers.workspace_workflow_fixtures import (
    review_output,
    write_fixture,
)


def review_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="WorkspaceReviewGap",
        worktrees={"implementation": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                depth=2,
                worktree="implementation",
                providers=[
                    ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="alpha", role=ProviderRole.REVIEWER),
                ],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content="Implement and review {{file:src/app.txt}}",
                    )
                ],
            ),
            WorkflowNode(
                id="consume",
                mode="sequential",
                needs=["implement"],
                worktree="implementation",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role="shared", content="Read {{file:src/app.txt}}")
                ],
            ),
        ],
    )


def write_review_fixtures(fixtures: Path) -> None:
    for round_num in (1, 2):
        write_fixture(
            fixtures,
            "implement",
            f"executor-round-{round_num}.md",
            "# Candidate\n\nInitial implementation updates `src/app.txt`.\n",
            sidecar={
                "workspace_mutations": [
                    {"path": "src/app.txt", "content": "first candidate\n"}
                ]
            },
        )
    write_fixture(
        fixtures,
        "implement",
        "executor-round-3.md",
        "# Final candidate\n\nResolved review issues in `src/app.txt`.\n",
        sidecar={
            "required_prompt_contains": ["first candidate"],
            "workspace_mutations": [
                {"path": "src/app.txt", "content": "final candidate\n"}
            ],
        },
    )
    write_fixture(
        fixtures,
        "implement",
        "reviewer-round-1.md",
        review_output("CHANGES_REQUESTED", "- Fix the remaining bug."),
    )
    write_fixture(
        fixtures,
        "implement",
        "reviewer-round-3.md",
        review_output("NO_FINDINGS", "None"),
    )
