from pathlib import Path

import pytest

from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.validation import (
    validate_workflow_plan,
)
from crewplane.version import SCHEMA_VERSION
from tests.unit.core.workflow_composition.workflow_composition_imports_support import (
    write_import_workflow,
)


def test_import_with_rejects_unused_parameters(tmp_path: Path) -> None:
    root = tmp_path
    module = root / "module.task.md"
    workflow = root / "root.task.md"

    write_import_workflow(
        module,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Module",
            "nodes:",
            "  - id: plan",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## plan",
            "",
            "No params here.",
        ],
    )
    write_import_workflow(
        workflow,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Root",
            "imports:",
            "  - path: module.task.md",
            "    as: auth",
            "    with:",
            "      module_name: payments-auth",
            "nodes:",
            "  - id: summary",
            "    mode: sequential",
            "    needs: [auth.plan]",
            "    providers: [alpha]",
            "---",
            "",
            "## summary",
            "",
            "done",
        ],
    )

    with pytest.raises(ValueError, match="unused parameter"):
        load_tasks_with_sources(workflow, project_root=root)


def test_import_with_rejects_shadowed_unused_parameters(tmp_path: Path) -> None:
    root = tmp_path
    leaf = root / "leaf.task.md"
    module = root / "module.task.md"
    workflow = root / "root.task.md"

    write_import_workflow(
        leaf,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Leaf",
            "nodes:",
            "  - id: plan",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## plan",
            "",
            "Leaf {{param:module_name}}",
        ],
    )
    write_import_workflow(
        module,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Module",
            "imports:",
            "  - path: leaf.task.md",
            "    as: leaf",
            "    with:",
            "      module_name: inner",
            "nodes:",
            "  - id: finalize",
            "    mode: sequential",
            "    needs: [leaf.plan]",
            "    providers: [alpha]",
            "---",
            "",
            "## finalize",
            "",
            "Finalize {{leaf.plan.output}}",
        ],
    )
    write_import_workflow(
        workflow,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Root",
            "imports:",
            "  - path: module.task.md",
            "    as: auth",
            "    with:",
            "      module_name: outer",
            "nodes:",
            "  - id: summary",
            "    mode: sequential",
            "    needs: [auth.finalize]",
            "    providers: [alpha]",
            "---",
            "",
            "## summary",
            "",
            "Summary",
        ],
    )

    with pytest.raises(ValueError, match="unused parameter"):
        load_tasks_with_sources(workflow, project_root=root)


def test_import_cycle_detection(tmp_path: Path) -> None:
    root = tmp_path
    workflow_a = root / "a.task.md"
    workflow_b = root / "b.task.md"

    write_import_workflow(
        workflow_a,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Workflow A",
            "imports:",
            "  - path: b.task.md",
            "    as: b",
            "nodes:",
            "  - id: a.node",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## a.node",
            "",
            "A",
        ],
    )
    write_import_workflow(
        workflow_b,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Workflow B",
            "imports:",
            "  - path: a.task.md",
            "    as: a",
            "nodes:",
            "  - id: b.node",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## b.node",
            "",
            "B",
        ],
    )

    with pytest.raises(ValueError, match="cycle"):
        load_tasks_with_sources(workflow_a, project_root=root)


def test_composition_rejects_node_id_collision(tmp_path: Path) -> None:
    root = tmp_path
    module = root / "module.task.md"
    workflow = root / "root.task.md"

    write_import_workflow(
        module,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Module",
            "nodes:",
            "  - id: plan",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## plan",
            "",
            "Plan",
        ],
    )
    write_import_workflow(
        workflow,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Root",
            "imports:",
            "  - path: module.task.md",
            "    as: auth",
            "nodes:",
            "  - id: auth.plan",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## auth.plan",
            "",
            "Conflict",
        ],
    )

    with pytest.raises(ValueError, match="Node ID collision"):
        load_tasks_with_sources(workflow, project_root=root)


def test_imported_schema_mismatch_fails(tmp_path: Path) -> None:
    root = tmp_path
    module = root / "module.task.md"
    workflow = root / "root.task.md"

    write_import_workflow(
        module,
        [
            "---",
            'schema_version: "99.0"',
            "name: Module",
            "nodes:",
            "  - id: plan",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## plan",
            "",
            "Plan",
        ],
    )
    write_import_workflow(
        workflow,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Root",
            "imports:",
            "  - path: module.task.md",
            "    as: auth",
            "nodes:",
            "  - id: summary",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## summary",
            "",
            "Summary",
        ],
    )

    with pytest.raises(ValueError, match="Unsupported workflow schema version"):
        load_tasks_with_sources(workflow, project_root=root)


def test_output_reference_requires_upstream_dependency_across_import_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path
    module = root / "module.task.md"
    workflow = root / "root.task.md"

    write_import_workflow(
        module,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Module",
            "nodes:",
            "  - id: plan",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## plan",
            "",
            "Plan",
        ],
    )
    write_import_workflow(
        workflow,
        [
            "---",
            f'schema_version: "{SCHEMA_VERSION}"',
            "name: Root",
            "imports:",
            "  - path: module.task.md",
            "    as: auth",
            "nodes:",
            "  - id: summary",
            "    mode: sequential",
            "    providers: [alpha]",
            "---",
            "",
            "## summary",
            "",
            "Use {{auth.plan.output}}",
        ],
    )

    workflow_plan = load_tasks_with_sources(
        workflow,
        project_root=root,
    ).workflow
    with pytest.raises(ValueError, match="not an upstream dependency"):
        validate_workflow_plan(workflow_plan)
