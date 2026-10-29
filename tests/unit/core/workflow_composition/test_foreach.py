from pathlib import Path

import pytest

from crewplane.core.preflight.source import load_workflow_source_for_preflight
from tests.helpers.foreach import (
    compile_foreach_file,
    copy_foreach_fixture,
    workflow_document,
    write_workflow_document,
)


def test_imported_parent_rewrites_callers_once_and_keeps_child_names_distinct(
    tmp_path: Path,
) -> None:
    path = copy_foreach_fixture(tmp_path)
    root = tmp_path / "root.md"
    write_workflow_document(
        root,
        {"name": "Root", "nodes": [], "imports": [{"path": path.name, "as": "outer"}]},
        "",
    )
    preview = compile_foreach_file(root)
    assert not preview.has_errors(), preview.diagnostics
    template = preview.foreach_templates[0]
    assert template.group_id == "outer.chunks"
    assert template.items == "{{outer.planner.findings}}"
    assert template.shared_bindings == {"shared": "outer.request"}
    analyzer = next(node for node in template.nodes if node.id.endswith(".analyze"))
    assert set(analyzer.dependencies) == {"outer.chunks.template.item", "outer.request"}
    assert all("outer.outer" not in str(edge) for edge in template.dependency_graph)


@pytest.mark.parametrize(
    "bindings,item_input",
    [
        ({"missing": "request"}, "item"),
        ({"item": "request"}, "item"),
        ({"alias": "request"}, "item"),
    ],
)
def test_invalid_or_aliased_child_bindings_rejected(
    tmp_path: Path, bindings: dict[str, str], item_input: str
) -> None:
    path = copy_foreach_fixture(tmp_path)
    child, body = workflow_document(tmp_path / "child.md")
    child["inputs"]["alias"] = "item"
    write_workflow_document(tmp_path / "child.md", child, body)
    parent, body = workflow_document(path)
    parent["nodes"][2].update(inputs=bindings, item_input=item_input)
    write_workflow_document(path, parent, body)
    with pytest.raises(ValueError, match="unknown child input|same child node"):
        load_workflow_source_for_preflight(path, tmp_path)


def test_distinct_inputs_may_share_a_caller_and_preserve_findings_keys(
    tmp_path: Path,
) -> None:
    path = copy_foreach_fixture(tmp_path)
    child, body = workflow_document(tmp_path / "child.md")
    child["inputs"]["extra"] = "extra"
    child["nodes"].append(
        {"id": "extra", "mode": "input", "source": "{{file:missing-extra}}"}
    )
    child["imports"][0]["inputs"]["request"] = "extra"
    write_workflow_document(tmp_path / "child.md", child, body)
    parent, body = workflow_document(path)
    parent["nodes"][2]["inputs"] = {"request": "planner", "extra": "planner"}
    write_workflow_document(path, parent, body)
    analyzer, body = workflow_document(tmp_path / "analyze.md")
    write_workflow_document(
        tmp_path / "analyze.md",
        analyzer,
        body.replace("{{shared.output}}", "{{shared.findings}}"),
    )
    preview = compile_foreach_file(path)
    assert not preview.has_errors(), preview.diagnostics
    template = preview.foreach_templates[0]
    assert set(template.shared_bindings.values()) == {"planner"}
    assert any(
        edge.source_node == "planner" and edge.artifact_name == "findings"
        for edge in template.dependency_graph
    )
    assert len(template.nodes) == 2


@pytest.mark.parametrize("path_kind", ["cycle", "outside", "nested"])
def test_fixed_children_reject_cycles_containment_and_nested_iteration(
    tmp_path: Path, path_kind: str
) -> None:
    path = copy_foreach_fixture(tmp_path)
    payload, body = workflow_document(path)
    if path_kind == "outside":
        payload["nodes"][2]["workflow"] = "../outside.md"
    elif path_kind == "cycle":
        payload["nodes"][2]["workflow"] = "parent.md"
        payload["inputs"] = {"item": "request", "request": "request"}
        payload["nodes"][2]["inputs"] = {}
    else:
        nested, nested_body = workflow_document(tmp_path / "analyze.md")
        nested["nodes"].append(
            {
                "id": "nested",
                "mode": "foreach",
                "workflow": "child.md",
                "item_input": "item",
                "items": [],
                "max_items": 1,
            }
        )
        write_workflow_document(
            tmp_path / "analyze.md", nested, nested_body + "\n## nested\n"
        )
    write_workflow_document(path, payload, body)
    with pytest.raises(ValueError, match="cycle|outside project root|nested foreach"):
        load_workflow_source_for_preflight(path, tmp_path)


def test_unbound_fallback_is_required_but_bound_fallback_is_not(tmp_path: Path) -> None:
    path = copy_foreach_fixture(tmp_path)
    assert not compile_foreach_file(path).has_errors()
    payload, body = workflow_document(path)
    payload["nodes"][2]["inputs"] = {}
    write_workflow_document(path, payload, body)
    preview = compile_foreach_file(path)
    assert preview.has_errors()
    assert "absent-request.txt" in str(preview.diagnostics)


def test_parameter_inheritance_override_and_unused_checks(tmp_path: Path) -> None:
    path = copy_foreach_fixture(tmp_path)
    root = tmp_path / "root.md"
    write_workflow_document(
        root,
        {
            "name": "Root",
            "nodes": [],
            "imports": [{"path": path.name, "as": "outer", "with": {"style": "brief"}}],
        },
        "",
    )
    with pytest.raises(ValueError, match="unused|not consumed"):
        load_workflow_source_for_preflight(root, tmp_path)
    parent, body = workflow_document(path)
    parent["nodes"][2].pop("with")
    write_workflow_document(path, parent, body)
    preview = compile_foreach_file(root)
    assert not preview.has_errors(), preview.diagnostics
    assert preview.foreach_templates[0].parameters == {"style": "brief"}
