import hashlib
from pathlib import Path

import pytest

from crewplane.core.workflow.composition.traversal import WorkflowComposer
from tests.helpers.foreach import (
    copy_foreach_fixture,
    workflow_document,
    write_workflow_document,
)


@pytest.mark.parametrize("failure_stage", ["child_traversal", "parameter_validation"])
def test_foreach_failure_clears_active_group_and_retains_partial_metadata(
    tmp_path: Path, failure_stage: str
) -> None:
    path = copy_foreach_fixture(tmp_path)
    if failure_stage == "child_traversal":
        child_path = tmp_path / "analyze.md"
        child, body = workflow_document(child_path)
        child["nodes"].append(
            {
                "id": "nested",
                "mode": "foreach",
                "workflow": "missing.md",
                "item_input": "item",
                "items": [],
                "max_items": 1,
            }
        )
        write_workflow_document(child_path, child, body + "\n## nested\n")
        expected_error = (
            "Foreach 'chunks' cannot contain nested foreach "
            "'chunks.template.part.nested'"
        )
    else:
        parent, body = workflow_document(path)
        parent["nodes"][2]["with"]["unused"] = "unused"
        write_workflow_document(path, parent, body)
        expected_error = (
            f"Workflow import 'chunks' in '{path}' defines unused parameter(s): unused"
        )

    composer = WorkflowComposer(tmp_path)
    with pytest.raises(ValueError) as caught:
        composer.compose(path)
    assert str(caught.value) == expected_error

    recovery_child = tmp_path / "recovery-child.md"
    write_workflow_document(
        recovery_child,
        {
            "name": "Recovery child",
            "inputs": {"item": "item"},
            "nodes": [
                {"id": "item", "mode": "input", "source": "{{file:missing.json}}"},
                {"id": "analyze", "mode": "parallel", "needs": ["item"]},
            ],
        },
        "## analyze\nRead {{item.output}}.\n",
    )
    recovery = tmp_path / "recovery.md"
    write_workflow_document(
        recovery,
        {
            "name": "Recovery",
            "nodes": [
                {
                    "id": "recovery",
                    "mode": "foreach",
                    "workflow": recovery_child.name,
                    "item_input": "item",
                    "items": [],
                    "max_items": 1,
                }
            ],
        },
        "## recovery\n",
    )

    document = composer.compose(recovery)

    assert [node["id"] for node in document.workflow_payload["nodes"]] == ["recovery"]
    assert list(document.foreach_templates) == ["recovery"]
    assert "chunks" in document.node_source_paths
    assert [record.path for record in document.source_records] == [
        path,
        tmp_path / "child.md",
        tmp_path / "analyze.md",
        recovery,
        recovery_child,
    ]
    template = document.foreach_templates["recovery"]
    assert template.item_node_id == "recovery.template.item"
    assert [node.payload.id for node in template.nodes] == [
        "recovery.template.item",
        "recovery.template.analyze",
    ]


def test_composer_retains_cached_sources_and_collision_tracking(tmp_path: Path) -> None:
    path = copy_foreach_fixture(tmp_path)
    composer = WorkflowComposer(tmp_path)
    document = composer.compose(path)
    source_records = list(document.source_records)
    original = path.read_bytes()
    assert source_records[0].sha256 == hashlib.sha256(original).hexdigest()
    path.write_bytes(b"invalid replacement")
    empty = tmp_path / "empty.md"
    write_workflow_document(empty, {"name": "Empty", "nodes": []}, "")

    later = composer.compose(empty)

    assert later.workflow_payload["nodes"] == []
    assert later.source_records[:-1] == source_records
    assert later.node_source_paths == document.node_source_paths
    assert later.foreach_templates == document.foreach_templates
    assert document.source_records == source_records
    with pytest.raises(ValueError, match="Node ID collision.*'request'"):
        composer.compose(path)
    assert path.read_bytes() == b"invalid replacement"
