from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.core.preflight.foreach.compilation import (
    compilation_source,
    finish_foreach_preview,
    inventory_workflow,
)
from crewplane.core.preflight.source import (
    PreflightWorkflowSource,
    load_workflow_source_for_preflight,
)
from crewplane.core.prompt_segments import PromptSegment
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan
from crewplane.core.workspace.policy import WorktreeDeclaration
from tests.helpers.foreach import (
    compile_foreach_file,
    copy_foreach_fixture,
    foreach_config,
)


@pytest.mark.parametrize(
    "collision, message",
    [
        ("node", "Foreach 'chunks' reserves node namespace 'chunks.'"),
        ("needs", "Foreach 'chunks' reserves node namespace 'chunks.'"),
        ("prompt", "Parent prompts cannot reference generated namespace 'chunks.'"),
        ("worktree", "Foreach 'chunks' reserves worktree namespace 'chunks.'"),
    ],
)
def test_namespace_errors_precede_collection_validation(
    tmp_path: Path, collision: str, message: str
) -> None:
    path = copy_foreach_fixture(tmp_path)
    source = load_workflow_source_for_preflight(path, tmp_path)
    source.workflow.nodes[2].items = "{{planner.output}}"
    reserved = WorkflowNode(id="chunks.authored", mode="input", source="unused")
    if collision in {"node", "needs"}:
        source.workflow.nodes.append(reserved)
    if collision == "needs":
        source.workflow.nodes[0].needs = [reserved.id]
    if collision == "prompt":
        source.workflow.nodes[1].prompt_segments = [
            PromptSegment(role="shared", content="{{chunks.generated.output}}")
        ]
        source.workflow.nodes.append(reserved)
    if collision == "worktree":
        source.workflow.worktrees["chunks.authored"] = WorktreeDeclaration(
            kind="worktree"
        )
    before = source.workflow.model_copy(deep=True)

    with pytest.raises(ValueError) as error:
        compilation_source(source, foreach_config())

    assert str(error.value) == message
    assert source.workflow == before


def test_concurrency_and_composition_errors_precede_namespace_validation(
    tmp_path: Path,
) -> None:
    path = copy_foreach_fixture(tmp_path)
    source = load_workflow_source_for_preflight(path, tmp_path)
    source.workflow.worktrees["chunks.authored"] = WorktreeDeclaration(kind="worktree")
    source = replace(source, foreach_templates={})

    with pytest.raises(ValueError) as error:
        compilation_source(source, foreach_config(max_concurrent_nodes=None))
    assert str(error.value) == (
        "Foreach workflows require explicit finite settings.max_concurrent_nodes"
    )
    with pytest.raises(ValueError) as error:
        compilation_source(source, foreach_config())
    assert str(error.value) == (
        "Foreach 'chunks' requires a composed Markdown child workflow"
    )


@pytest.mark.parametrize(
    "items, findings, message",
    [
        (
            "{{planner.output}}",
            True,
            "Foreach 'chunks' collection output must come from an input or foreach node",
        ),
        (
            "{{planner.findings}}",
            False,
            "Foreach 'chunks' requires available single-executor findings from 'planner'",
        ),
        (
            "{{planner.findings}}",
            True,
            "Foreach 'chunks' sources must be ancestors through authored needs: planner, request",
        ),
    ],
)
def test_collection_errors_precede_ancestry_and_report_sorted_sources(
    tmp_path: Path, items: str, findings: bool, message: str
) -> None:
    path = copy_foreach_fixture(tmp_path)
    source = load_workflow_source_for_preflight(path, tmp_path)
    source.workflow.nodes[1].findings = findings
    source.workflow.nodes[2].items = items
    source.workflow.nodes[2].needs = []

    with pytest.raises(ValueError) as error:
        compilation_source(source, foreach_config())

    assert str(error.value) == message


def test_sources_without_foreach_are_reused() -> None:
    source = PreflightWorkflowSource.from_workflow(WorkflowPlan(name="Empty", nodes=[]))

    assert inventory_workflow(source) is source.workflow
    assert (
        compilation_source(source, foreach_config(max_concurrent_nodes=None)) is source
    )


@pytest.mark.parametrize(
    "parent_trees, parent_selector, child_selector, implicit_selector, expected_parent, expected_child",
    [
        ([], None, None, None, "none", "none"),
        (["parent"], None, None, None, "parent", "none"),
        (["first", "second"], None, None, "child", "none", "child"),
        (["first", "second"], "second", "explicit", "child", "second", "explicit"),
        (["parent"], "none", "none", "child", "none", "none"),
    ],
)
def test_inventory_preserves_worktree_precedence_order_and_copy_boundaries(
    tmp_path: Path,
    parent_trees: list[str],
    parent_selector: str | None,
    child_selector: str | None,
    implicit_selector: str | None,
    expected_parent: str,
    expected_child: str,
) -> None:
    path = copy_foreach_fixture(tmp_path)
    source = load_workflow_source_for_preflight(path, tmp_path)
    source.workflow.worktrees = {
        name: WorktreeDeclaration(kind="snapshot") for name in parent_trees
    }
    source.workflow.nodes[1].worktree = parent_selector
    child = source.foreach_templates["chunks"]
    executable = next(
        node.payload for node in child.nodes if node.payload.mode != "input"
    )
    executable.worktree = child_selector
    child = replace(
        child,
        nodes=[
            replace(node, implicit_worktree_selector=implicit_selector)
            for node in child.nodes
        ],
        worktrees={"child": WorktreeDeclaration(kind="worktree")},
    )
    source = replace(source, foreach_templates={"chunks": child})
    originals = [*source.workflow.nodes, *(node.payload for node in child.nodes)]
    before = [node.model_copy(deep=True) for node in originals]

    result = inventory_workflow(source)

    assert result is not source.workflow
    assert [node.id for node in result.nodes] == [node.id for node in originals]
    assert result.nodes[1].worktree == expected_parent
    assert (
        next(node for node in result.nodes if node.id == executable.id).worktree
        == expected_child
    )
    assert list(result.worktrees) == [*parent_trees, "child"]
    assert list(source.workflow.worktrees) == parent_trees
    assert originals == before
    for original, projected in zip(originals, result.nodes, strict=True):
        if original.mode in {"input", "foreach"}:
            assert projected is original
        else:
            assert projected is not original
            assert projected.needs is original.needs


def test_preview_partition_preserves_record_order_identity_and_global_state(
    tmp_path: Path,
) -> None:
    path = copy_foreach_fixture(tmp_path)
    source = load_workflow_source_for_preflight(path, tmp_path)
    preview = compile_foreach_file(path)
    assert not preview.has_errors(), preview.diagnostics
    (template,) = preview.foreach_templates
    inventory = preview.model_copy(
        update={
            "foreach_templates": [],
            "nodes": [*reversed(template.nodes), *reversed(preview.nodes)],
            "execution_order": [*template.execution_order, *preview.execution_order],
            "render_plans": [*template.render_plans, *reversed(preview.render_plans)],
            "workspace_file_locators": [
                *template.workspace_file_locators,
                *preview.workspace_file_locators,
            ],
            "token_catalog": [
                *template.token_catalog,
                *reversed(preview.token_catalog),
            ],
            "dependency_graph": [
                *template.dependency_graph,
                *reversed(preview.dependency_graph),
            ],
        }
    )
    before = inventory.model_copy(deep=True)

    result = finish_foreach_preview(source, inventory)

    assert inventory == before
    assert result is not inventory
    assert result.nodes == list(reversed(preview.nodes))
    assert result.execution_order == preview.execution_order
    assert result.render_plans == list(reversed(preview.render_plans))
    assert result.token_catalog == list(reversed(preview.token_catalog))
    assert result.dependency_graph == list(reversed(preview.dependency_graph))
    assert result.foreach_templates == [template]
    assert template.semantic_digest() == template.template_digest
    assert any(edge.source_node == "request" for edge in template.dependency_graph)
    for node in result.nodes:
        assert any(node is original for original in inventory.nodes)
    for node in result.foreach_templates[0].nodes:
        assert any(node is original for original in inventory.nodes)
    assert result.static_resources is inventory.static_resources
    assert result.static_file_payloads is inventory.static_file_payloads
    assert result.secret_context is inventory.secret_context
    assert result.workflow_signature == inventory.workflow_signature
