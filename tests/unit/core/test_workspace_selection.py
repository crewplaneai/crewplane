import pytest

from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan
from crewplane.core.workspace.selection import (
    latest_lineage_source,
    logical_workspace_selections,
    selected_worktree_name,
)
from tests.helpers.platforms import requires_workspace_support
from tests.helpers.workspace_preflight import workspace_config

pytestmark = requires_workspace_support


@pytest.mark.parametrize("declarations", [(), ("primary",), ("primary", "secondary")])
@pytest.mark.parametrize("selector", [None, "none", "primary", "secondary"])
@pytest.mark.parametrize("mode", ["input", "sequential"])
def test_workspace_selection_precedence(
    declarations: tuple[str, ...], selector: str | None, mode: str
) -> None:
    node = WorkflowNode.model_construct(id="node", mode=mode, worktree=selector)
    workflow = WorkflowPlan.model_construct(
        worktrees=dict.fromkeys(declarations), nodes=[node]
    )
    expected_by_selector = {
        None: declarations[0] if len(declarations) == 1 else None,
        "none": None,
        "primary": "primary",
        "secondary": "secondary",
    }

    selected = selected_worktree_name(workflow, node)

    assert selected == (None if mode == "input" else expected_by_selector[selector])


@pytest.mark.parametrize(
    ("counts", "order", "expected"),
    [
        ({}, {}, None),
        ({"only": 0}, {"only": 0}, "only"),
        ({"deep": 2, "late": 1}, {"deep": 0, "late": 1}, "deep"),
        ({"early": 2, "late": 2}, {"early": 0, "late": 1}, "late"),
        ({"early": 2, "late": 2}, {"early": 1, "late": 0}, "early"),
    ],
)
def test_lineage_source_ranks_only_eligible_candidates(counts, order, expected):
    assert latest_lineage_source(counts, order) == expected


def test_logical_resolution_omits_inputs_and_preserves_raw_setup_commands() -> None:
    config = workspace_config(
        {
            "enabled": True,
            "setup_profiles": {
                "bootstrap": {"run": [["setup", "--api-key", "example-value"]]}
            },
        }
    )
    workflow = WorkflowPlan.model_validate(
        {
            "name": "selection",
            "worktrees": {
                "primary": {"kind": "worktree", "setup_profile": "bootstrap"}
            },
            "nodes": [
                {"id": "context", "mode": "input", "source": "{{file:context.md}}"},
                {"id": "implement", "mode": "sequential"},
                {"id": "inspect", "mode": "sequential", "worktree": "none"},
            ],
        }
    )

    selections = logical_workspace_selections(workflow, config)

    assert set(selections) == {"implement", "inspect"}
    assert selections["implement"].logical_worktree_name == "primary"
    assert selections["implement"].setup_profile == "bootstrap"
    assert selections["implement"].setup_commands == (
        ("setup", "--api-key", "example-value"),
    )
    assert selections["inspect"].enabled is False
    assert selections["inspect"].setup_commands == ()
