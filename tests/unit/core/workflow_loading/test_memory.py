from pathlib import Path

import pytest
from pydantic import ValidationError

from crewplane.core.workflow.markdown import parse_workflow_markdown_document
from crewplane.core.workflow.markdown.models import (
    WorkflowFrontmatter,
    WorkflowNodeConfig,
)
from crewplane.core.workflow.models import (
    WorkflowNode,
    WorkflowPlan,
    workflow_node_payload_dict,
    workflow_payload_dict,
)


@pytest.mark.parametrize("memory", [True, False])
def test_memory_declarations_roundtrip_without_losing_false(memory: bool) -> None:
    document = parse_workflow_markdown_document(
        Path("task.md"),
        f"---\nname: Task\nmemory: {str(memory).lower()}\nnodes:\n"
        f"  - {{id: work, mode: parallel, providers: [alpha], memory: {str(memory).lower()}}}\n"
        "---\n## work\nDo work.\n",
    )
    assert document.payload["memory"] is memory
    assert document.payload["nodes"][0]["memory"] is memory
    payload = dict(document.payload)
    payload.pop("imports")
    workflow = WorkflowPlan.model_validate(payload)
    assert workflow_payload_dict(workflow)["memory"] is memory
    assert workflow_node_payload_dict(workflow.nodes[0])["memory"] is memory


def test_omitted_memory_stays_undeclared_until_composition() -> None:
    node = WorkflowNode(id="work", mode="parallel")
    workflow = WorkflowPlan(name="Task", nodes=[node])
    assert node.memory is None
    assert workflow.memory is None
    assert "memory" not in workflow_payload_dict(workflow)
    assert "memory" not in workflow_node_payload_dict(node)


@pytest.mark.parametrize("value", [None, "true", "false", 0, 1, 1.0, [], {}])
@pytest.mark.parametrize("model", [WorkflowPlan, WorkflowFrontmatter])
def test_workflow_memory_rejects_non_booleans(model, value: object) -> None:
    with pytest.raises(ValidationError, match="memory"):
        model.model_validate({"name": "Task", "nodes": [], "memory": value})


@pytest.mark.parametrize("value", [None, "true", "false", 0, 1, 1.0, [], {}])
@pytest.mark.parametrize("model", [WorkflowNode, WorkflowNodeConfig])
def test_node_memory_rejects_non_booleans(model, value: object) -> None:
    with pytest.raises(ValidationError, match="memory"):
        model.model_validate({"id": "work", "mode": "parallel", "memory": value})


@pytest.mark.parametrize("memory", [True, False])
@pytest.mark.parametrize("model", [WorkflowNode, WorkflowNodeConfig])
@pytest.mark.parametrize("mode", ["input", "foreach"])
def test_input_and_foreach_cannot_declare_memory(
    model, mode: str, memory: bool
) -> None:
    declaration = {"id": "source", "mode": mode, "memory": memory}
    if mode == "foreach":
        declaration.update(workflow="child.md", item_input="item", items=[])
    else:
        declaration["source"] = "{{file:input.md}}"
    with pytest.raises(ValidationError, match="memory"):
        model.model_validate(declaration)
