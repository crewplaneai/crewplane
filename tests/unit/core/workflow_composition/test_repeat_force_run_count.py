from pathlib import Path

import pytest

from crewplane.core.config import Config, Settings
from crewplane.core.preflight import load_workflow_source_for_preflight
from crewplane.core.workflow.models import render_prompt_for_role
from crewplane.core.workflow.validation import (
    collect_workspace_validation_diagnostics,
    validate_workflow_plan,
)
from crewplane.version import SCHEMA_VERSION


def write_nested_workflows(root: Path, count: int | None) -> Path:
    (root / "leaf.task.md").write_text(
        "---\nname: Leaf\nrepeat_force_run_count: 5\n"
        "nodes:\n  - id: scan\n    mode: sequential\n    providers: [alpha]\n"
        "---\n## scan\nScan.\n",
        encoding="utf-8",
    )
    (root / "child.task.md").write_text(
        "---\nname: Child\nrepeat_force_run_count: 4\n"
        "imports:\n  - path: leaf.task.md\n    as: leaf\n"
        "nodes:\n  - id: fix\n    mode: sequential\n    providers: [alpha]\n"
        "    needs: [leaf.scan]\n---\n## fix\nFix {{leaf.scan.output}}.\n",
        encoding="utf-8",
    )
    field = f"repeat_force_run_count: {count}\n" if count is not None else ""
    path = root / "root.task.md"
    path.write_text(
        f"---\nname: Root\n{field}"
        "imports:\n  - path: child.task.md\n    as: child\n"
        "nodes:\n  - id: verify\n    mode: sequential\n    providers: [alpha]\n"
        "    needs: [child.fix]\n---\n## verify\nVerify {{child.fix.output}}.\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("count", [None, 1, 3])
def test_only_root_controls_repetition(tmp_path: Path, count: int | None) -> None:
    path = write_nested_workflows(tmp_path, count)
    source = load_workflow_source_for_preflight(path, tmp_path)
    assert source.workflow.repeat_force_run_count == count
    assert source.composed_workflow.get("repeat_force_run_count") == count
    assert ("repeat_force_run_count" in source.composed_workflow) == (count is not None)
    assert len(source.referenced_workflows) == 3
    nodes = source.workflow.nodes
    assert [node.id for node in nodes] == ["child.leaf.scan", "child.fix", "verify"]
    assert [node.needs for node in nodes] == [[], ["child.leaf.scan"], ["child.fix"]]
    assert "{{child.leaf.scan.output}}" in render_prompt_for_role(nodes[1], "executor")
    validate_workflow_plan(source.workflow)
    nodes[2].needs = []
    with pytest.raises(ValueError, match="upstream|dependencies|ancestor"):
        validate_workflow_plan(source.workflow)


@pytest.mark.parametrize("filename", ["child.task.md", "leaf.task.md"])
def test_imported_counts_are_still_validated(tmp_path: Path, filename: str) -> None:
    path = write_nested_workflows(tmp_path, None)
    imported = tmp_path / filename
    text = imported.read_text(encoding="utf-8")
    imported.write_text(
        text.replace("count: 4", "count: null").replace("count: 5", "count: true"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="repeat_force_run_count"):
        load_workflow_source_for_preflight(path, tmp_path)


@pytest.mark.parametrize("count", [None, 1, 3])
def test_workspace_policy_applies_after_composition(
    tmp_path: Path, count: int | None
) -> None:
    path = write_nested_workflows(tmp_path, count)
    child = tmp_path / "child.task.md"
    child.write_text(
        child.read_text().replace(
            "name: Child", "name: Child\nworktrees:\n  unused:\n    kind: snapshot"
        ),
        encoding="utf-8",
    )
    source = load_workflow_source_for_preflight(path, tmp_path)
    config = Config(
        version=SCHEMA_VERSION,
        agents={},
        settings=Settings(workspace={"enabled": True}),
    )
    diagnostics = collect_workspace_validation_diagnostics(source.workflow, config)
    assert any("repeat_force_run_count" in item.message for item in diagnostics) == (
        count is not None
    )
