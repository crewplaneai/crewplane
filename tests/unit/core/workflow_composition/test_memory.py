from pathlib import Path

import pytest

from crewplane.core.preflight.source import (
    PreflightWorkflowSource,
    load_workflow_source_for_preflight,
)
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan
from tests.helpers.foreach import (
    copy_foreach_fixture,
    workflow_document,
    write_workflow_document,
)


@pytest.mark.parametrize("root_memory", [None, False, True])
@pytest.mark.parametrize("child_memory", [None, False, True])
@pytest.mark.parametrize("node_memory", [None, False, True])
def test_imported_memory_resolves_node_defining_and_enclosing_defaults(
    tmp_path: Path,
    root_memory: bool | None,
    child_memory: bool | None,
    node_memory: bool | None,
) -> None:
    node = {"id": "work", "mode": "parallel", "providers": ["alpha"]}
    if node_memory is not None:
        node["memory"] = node_memory
    leaf = {"name": "Leaf", "nodes": [node]}
    write_workflow_document(tmp_path / "leaf.md", leaf, "## work\nDo work.\n")
    child = {
        "name": "Child",
        "nodes": [],
        "imports": [{"path": "leaf.md", "as": "leaf"}],
    }
    if child_memory is not None:
        child["memory"] = child_memory
    write_workflow_document(tmp_path / "child.md", child, "")
    root = {
        "name": "Root",
        "nodes": [],
        "imports": [{"path": "child.md", "as": "child"}],
    }
    if root_memory is not None:
        root["memory"] = root_memory
    path = tmp_path / "root.md"
    write_workflow_document(path, root, "")

    source = load_workflow_source_for_preflight(path, tmp_path)

    expected = next(
        value
        for value in (node_memory, child_memory, root_memory, False)
        if value is not None
    )
    assert source.workflow.nodes[0].id == "child.leaf.work"
    assert source.workflow.nodes[0].memory is expected
    assert source.composed_workflow["nodes"][0]["memory"] is expected


@pytest.mark.parametrize("parent_memory", [False, True])
@pytest.mark.parametrize("child_memory", [None, False, True])
def test_foreach_children_keep_memory_inheritance_while_resetting_worktrees(
    tmp_path: Path, parent_memory: bool, child_memory: bool | None
) -> None:
    path = copy_foreach_fixture(tmp_path)
    parent, body = workflow_document(path)
    parent["memory"] = parent_memory
    parent["worktrees"] = {"root": {"kind": "worktree"}}
    write_workflow_document(path, parent, body)
    child, body = workflow_document(tmp_path / "child.md")
    if child_memory is not None:
        child["memory"] = child_memory
    write_workflow_document(tmp_path / "child.md", child, body)

    source = load_workflow_source_for_preflight(path, tmp_path)

    template = source.foreach_templates["chunks"]
    expected = parent_memory if child_memory is None else child_memory
    providers = [node.payload for node in template.nodes if node.payload.providers]
    assert providers
    assert all(node.memory is expected for node in providers)
    assert all(node.worktree is None for node in providers)
    assert all(
        node.payload.memory is None
        for node in template.nodes
        if node.payload.mode == "input"
    )
    assert source.workflow.nodes[0].memory is None
    assert source.workflow.nodes[1].memory is parent_memory
    assert source.workflow.nodes[2].memory is None


def test_bound_input_removal_and_node_override_preserve_effective_memory(
    tmp_path: Path,
) -> None:
    path = copy_foreach_fixture(tmp_path)
    parent, body = workflow_document(path)
    parent["memory"] = True
    write_workflow_document(path, parent, body)
    analyzer, body = workflow_document(tmp_path / "analyze.md")
    analyzer["nodes"][-1]["memory"] = False
    write_workflow_document(tmp_path / "analyze.md", analyzer, body)

    source = load_workflow_source_for_preflight(path, tmp_path)

    template = source.foreach_templates["chunks"]
    assert template.shared_bindings == {"shared": "request"}
    assert all(node.payload.id != "chunks.template.shared" for node in template.nodes)
    analyzer_node = next(
        node.payload for node in template.nodes if node.payload.id.endswith(".analyze")
    )
    assert analyzer_node.memory is False
    assert "request" in analyzer_node.needs


@pytest.mark.parametrize("workflow_memory", [None, False, True])
@pytest.mark.parametrize("node_memory", [None, False, True])
def test_programmatic_source_resolves_the_same_defaults(
    workflow_memory: bool | None, node_memory: bool | None
) -> None:
    node = {"id": "work", "mode": "parallel"}
    if node_memory is not None:
        node["memory"] = node_memory
    payload = {
        "name": "Task",
        "nodes": [
            node,
            {"id": "input", "mode": "input", "source": "{{file:input.txt}}"},
        ],
    }
    if workflow_memory is not None:
        payload["memory"] = workflow_memory
    workflow = WorkflowPlan.model_validate(payload)

    source = PreflightWorkflowSource.from_workflow(workflow)

    expected = next(
        value for value in (node_memory, workflow_memory, False) if value is not None
    )
    assert source.workflow.nodes[0].memory is expected
    assert source.composed_workflow["nodes"][0]["memory"] is expected
    assert source.workflow.nodes[1].memory is None
    assert workflow.nodes[0].memory is node_memory


def test_programmatic_source_preserves_already_composed_false_override() -> None:
    workflow = WorkflowPlan(
        name="Task",
        memory=True,
        nodes=[WorkflowNode(id="work", mode="parallel", memory=False)],
    )
    source = PreflightWorkflowSource.from_workflow(workflow)
    assert source.workflow.nodes[0].memory is False


def test_legacy_yaml_preflight_source_resolves_memory_defaults(tmp_path: Path) -> None:
    path = tmp_path / "task.yaml"
    path.write_text(
        "name: Task\nmemory: true\nnodes:\n"
        "  - {id: work, mode: parallel, providers: [{provider: alpha}]}\n",
        encoding="utf-8",
        newline="\n",
    )
    source = load_workflow_source_for_preflight(path, tmp_path)
    assert source.workflow.nodes[0].memory is True
    assert source.composed_workflow["nodes"][0]["memory"] is True
