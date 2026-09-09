import pytest

from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan
from crewplane.core.workspace.selection import selected_worktree_name


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
