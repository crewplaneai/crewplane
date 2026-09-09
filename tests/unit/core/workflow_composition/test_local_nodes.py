from pathlib import Path

import pytest

from crewplane.core.workflow.composition.models import (
    CompositionContext,
    NodeSpec,
    ParamBinding,
    ParsedWorkflow,
)
from crewplane.core.workflow.composition.nodes import compose_local_node
from crewplane.core.workflow.models import PromptSegment, ProviderSpec, WorkflowNode
from crewplane.version import SCHEMA_VERSION


@pytest.mark.parametrize("input_node", [True, False])
def test_composition_preserves_explicit_fields_and_source_metadata(
    tmp_path: Path, input_node: bool
) -> None:
    payload = (
        WorkflowNode(id="local", mode="input", source="{{file:input.txt}}")
        if input_node
        else WorkflowNode(
            id="local",
            mode="sequential",
            providers=[ProviderSpec(provider="alpha")],
            prompt_segments=[PromptSegment(role="shared", content="{{param:goal}}")],
        )
    )
    source_path = tmp_path / "workflow.task.md"
    node = NodeSpec(
        payload,
        source_path,
        {"start_line": 5, "start_column": 0, "end_line": 7, "end_column": 0},
        (),
    )
    workflow = ParsedWorkflow(
        source_path, SCHEMA_VERSION, "test", "", {}, {}, (), (node,)
    )
    context = CompositionContext(
        workflow,
        "imported",
        {"goal": ParamBinding("resolved goal", "goal-binding")},
        {},
        (),
        implicit_worktree_selector="imported.worktree",
    )
    original_fields = payload.model_fields_set.copy()

    composed, consumed = compose_local_node(context, node)

    assert composed.payload.id == "imported.local"
    assert composed.payload.model_fields_set == original_fields | {
        "id",
        "needs",
        "worktree",
    }
    assert payload.model_fields_set == original_fields
    assert payload.id == "local"
    assert composed.source_path == source_path
    assert composed.source_span == node.source_span
    assert composed.prompt_segment_spans == node.prompt_segment_spans
    if input_node:
        assert "prompt_segments" not in composed.payload.model_fields_set
        assert composed.implicit_worktree_selector is None
        assert consumed == set()
    else:
        assert composed.payload.prompt_segments[0].content == "resolved goal"
        assert composed.implicit_worktree_selector == "imported.worktree"
        assert consumed == {"goal-binding"}
