import tempfile
from pathlib import Path

from crewplane.core.config import Config
from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.models import (
    WorkflowPlan,
)
from crewplane.core.workflow.validation import (
    collect_workflow_policy_diagnostics,
    validate_workflow_plan,
)
from crewplane.core.workflow.validation.workspace import (
    logical_workspace_selections,
)
from crewplane.version import SCHEMA_VERSION
from tests.unit.core.workflow_composition.workflow_composition_imports_support import (
    write_import_workflow,
)


def _workspace_config(clean_start: str = "strict") -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": {"cli_cmd": ["echo"]}},
        settings={"workspace": {"enabled": True, "clean_start": clean_start}},
    )


def _workspace_policy_messages(
    workflow: WorkflowPlan,
    clean_start: str = "strict",
) -> tuple[str, ...]:
    return tuple(
        diagnostic.message
        for diagnostic in collect_workflow_policy_diagnostics(
            workflow,
            _workspace_config(clean_start=clean_start),
        )
    )


def test_imported_nodes_inherit_root_single_worktree() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
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
                "Plan.",
            ],
        )
        write_import_workflow(
            workflow,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Root",
                "worktrees:",
                "  primary:",
                "    kind: worktree",
                "imports:",
                "  - path: module.task.md",
                "    as: module",
                "nodes: []",
                "---",
                "",
            ],
        )

        workflow_plan = load_tasks_with_sources(
            workflow,
            project_root=root,
        ).workflow

    assert workflow_plan.nodes[0].id == "module.plan"
    assert workflow_plan.nodes[0].worktree is None
    selections = logical_workspace_selections(
        workflow_plan,
        _workspace_config(),
    )
    assert selections["module.plan"].logical_worktree_name == "primary"


def test_imported_worktree_selector_is_namespace_rewritten() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        module = root / "module.task.md"
        workflow = root / "root.task.md"

        write_import_workflow(
            module,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Module",
                "worktrees:",
                "  implementation:",
                "    kind: worktree",
                "nodes:",
                "  - id: implement",
                "    mode: sequential",
                "    providers: [alpha]",
                "    worktree: implementation",
                "  - id: fix",
                "    mode: sequential",
                "    needs: [implement]",
                "    providers: [alpha]",
                "    worktree: implementation",
                "---",
                "",
                "## implement",
                "",
                "Implement",
                "",
                "## fix",
                "",
                "Fix {{implement.output}}",
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
                "nodes: []",
                "---",
            ],
        )

        validated = validate_workflow_plan(
            load_tasks_with_sources(workflow, project_root=root).workflow
        )

    assert [node.id for node in validated.nodes] == ["auth.implement", "auth.fix"]
    assert list(validated.worktrees) == ["auth.implementation"]
    assert validated.nodes[0].worktree == "auth.implementation"
    assert validated.nodes[1].worktree == "auth.implementation"
    assert validated.nodes[1].needs == ["auth.implement"]
    selections = logical_workspace_selections(validated, _workspace_config())
    assert selections["auth.fix"].logical_worktree_name == "auth.implementation"
    assert selections["auth.fix"].source_node_id == "auth.implement"


def test_imported_worktree_none_selector_stays_project_root() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        module = root / "module.task.md"
        workflow = root / "root.task.md"

        write_import_workflow(
            module,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Module",
                "worktrees:",
                "  implementation:",
                "    kind: worktree",
                "nodes:",
                "  - id: inspect",
                "    mode: sequential",
                "    providers: [alpha]",
                "    worktree: none",
                "---",
                "",
                "## inspect",
                "",
                "Inspect project root",
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
                "nodes: []",
                "---",
            ],
        )

        validated = validate_workflow_plan(
            load_tasks_with_sources(workflow, project_root=root).workflow
        )

    assert list(validated.worktrees) == ["auth.implementation"]
    assert validated.nodes[0].id == "auth.inspect"
    assert validated.nodes[0].worktree == "none"
    selection = logical_workspace_selections(
        validated,
        _workspace_config(),
    )["auth.inspect"]
    assert not selection.enabled
    assert selection.logical_worktree_name is None


def test_imported_implicit_worktree_selector_uses_namespaced_declaration() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        module = root / "module.task.md"
        workflow = root / "root.task.md"

        write_import_workflow(
            module,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Module",
                "worktrees:",
                "  implementation:",
                "    kind: worktree",
                "nodes:",
                "  - id: implement",
                "    mode: sequential",
                "    providers: [alpha]",
                "  - id: fix",
                "    mode: sequential",
                "    needs: [implement]",
                "    providers: [alpha]",
                "---",
                "",
                "## implement",
                "",
                "Implement",
                "",
                "## fix",
                "",
                "Fix {{implement.output}}",
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
                "nodes: []",
                "---",
            ],
        )

        validated = validate_workflow_plan(
            load_tasks_with_sources(workflow, project_root=root).workflow
        )

    assert list(validated.worktrees) == ["auth.implementation"]
    assert validated.nodes[0].worktree is None
    assert validated.nodes[1].worktree is None
    selections = logical_workspace_selections(
        validated,
        _workspace_config(clean_start="tracked_only"),
    )
    assert selections["auth.implement"].logical_worktree_name == "auth.implementation"
    assert selections["auth.implement"].clean_start == "tracked_only"
    assert selections["auth.fix"].source_node_id == "auth.implement"
    assert selections["auth.fix"].clean_start == "tracked_only"
    assert _workspace_policy_messages(validated, clean_start="tracked_only") == ()


def test_nested_import_inherits_nearest_parent_single_worktree() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        child = root / "child.task.md"
        parent = root / "parent.task.md"
        workflow = root / "root.task.md"

        write_import_workflow(
            child,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Child",
                "nodes:",
                "  - id: inspect",
                "    mode: sequential",
                "    providers: [alpha]",
                "---",
                "",
                "## inspect",
                "",
                "Inspect inherited worktree",
            ],
        )
        write_import_workflow(
            parent,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Parent",
                "worktrees:",
                "  implementation:",
                "    kind: worktree",
                "imports:",
                "  - path: child.task.md",
                "    as: child",
                "nodes: []",
                "---",
            ],
        )
        write_import_workflow(
            workflow,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Root",
                "imports:",
                "  - path: parent.task.md",
                "    as: auth",
                "nodes: []",
                "---",
            ],
        )

        validated = validate_workflow_plan(
            load_tasks_with_sources(workflow, project_root=root).workflow
        )

    assert list(validated.worktrees) == ["auth.implementation"]
    assert [node.id for node in validated.nodes] == ["auth.child.inspect"]
    assert validated.nodes[0].worktree is None
    selection = logical_workspace_selections(validated, _workspace_config())[
        "auth.child.inspect"
    ]
    assert selection.enabled
    assert selection.logical_worktree_name == "auth.implementation"
    assert _workspace_policy_messages(validated) == ()


def test_root_node_does_not_inherit_only_imported_worktree() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        module = root / "module.task.md"
        workflow = root / "root.task.md"

        write_import_workflow(
            module,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Module",
                "worktrees:",
                "  implementation:",
                "    kind: worktree",
                "nodes:",
                "  - id: implement",
                "    mode: sequential",
                "    providers: [alpha]",
                "---",
                "",
                "## implement",
                "",
                "Implement",
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
                "  - id: requirements",
                "    mode: input",
                '    source: "{{file:.crewplane/inputs/requirements.md}}"',
                "  - id: inspect",
                "    mode: sequential",
                "    needs: [requirements]",
                "    providers: [alpha]",
                "---",
                "",
                "## inspect",
                "",
                "Inspect without managed workspace",
            ],
        )

        validated = validate_workflow_plan(
            load_tasks_with_sources(workflow, project_root=root).workflow
        )

    assert [node.id for node in validated.nodes] == [
        "auth.implement",
        "requirements",
        "inspect",
    ]
    assert validated.nodes[0].worktree is None
    assert validated.nodes[1].worktree is None
    assert validated.nodes[2].worktree == "none"
    selections = logical_workspace_selections(validated, _workspace_config())
    assert selections["auth.implement"].logical_worktree_name == "auth.implementation"
    assert not selections["inspect"].enabled
    assert selections["inspect"].materialization == "project_root"
    assert _workspace_policy_messages(validated) == ()


def test_imported_modules_keep_local_single_worktree_inheritance() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        auth_module = root / "auth.task.md"
        billing_module = root / "billing.task.md"
        workflow = root / "root.task.md"

        for path, node_id in (
            (auth_module, "implement-auth"),
            (billing_module, "implement-billing"),
        ):
            write_import_workflow(
                path,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    f"name: {node_id}",
                    "worktrees:",
                    "  implementation:",
                    "    kind: worktree",
                    "nodes:",
                    f"  - id: {node_id}",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    f"## {node_id}",
                    "",
                    "Implement",
                ],
            )
        write_import_workflow(
            workflow,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Root",
                "imports:",
                "  - path: auth.task.md",
                "    as: auth",
                "  - path: billing.task.md",
                "    as: billing",
                "nodes: []",
                "---",
            ],
        )

        validated = validate_workflow_plan(
            load_tasks_with_sources(workflow, project_root=root).workflow
        )

    assert list(validated.worktrees) == [
        "auth.implementation",
        "billing.implementation",
    ]
    assert [node.worktree for node in validated.nodes] == [
        "auth.implementation",
        "billing.implementation",
    ]
    assert _workspace_policy_messages(validated) == ()


def test_root_node_keeps_local_single_worktree_inheritance_with_imports() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        module = root / "module.task.md"
        workflow = root / "root.task.md"

        write_import_workflow(
            module,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Module",
                "worktrees:",
                "  implementation:",
                "    kind: worktree",
                "nodes:",
                "  - id: implement",
                "    mode: sequential",
                "    providers: [alpha]",
                "---",
                "",
                "## implement",
                "",
                "Implement",
            ],
        )
        write_import_workflow(
            workflow,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Root",
                "worktrees:",
                "  scratch:",
                "    kind: snapshot",
                "imports:",
                "  - path: module.task.md",
                "    as: auth",
                "nodes:",
                "  - id: inspect",
                "    mode: sequential",
                "    providers: [alpha]",
                "---",
                "",
                "## inspect",
                "",
                "Inspect",
            ],
        )

        validated = validate_workflow_plan(
            load_tasks_with_sources(workflow, project_root=root).workflow
        )

    assert list(validated.worktrees) == ["auth.implementation", "scratch"]
    assert validated.nodes[0].worktree == "auth.implementation"
    assert validated.nodes[1].worktree == "scratch"
    assert _workspace_policy_messages(validated) == ()


def test_root_worktree_declaration_remains_workflow_scoped() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        workflow = root / "root.task.md"

        write_import_workflow(
            workflow,
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Root",
                "worktrees:",
                "  scratch:",
                "    kind: snapshot",
                "nodes:",
                "  - id: inspect",
                "    mode: sequential",
                "    providers: [alpha]",
                "---",
                "",
                "## inspect",
                "",
                "Inspect",
            ],
        )

        loaded = load_tasks_with_sources(workflow, project_root=root).workflow

    assert list(loaded.worktrees) == ["scratch"]
    assert loaded.nodes[0].worktree is None
    selections = logical_workspace_selections(loaded, _workspace_config())
    assert selections["inspect"].logical_worktree_name == "scratch"
    assert selections["inspect"].declaration_kind == "snapshot"
