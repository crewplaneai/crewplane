import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.core.config import Config
from orchestrator_cli.core.workflow_loader import load_tasks_with_sources
from orchestrator_cli.core.workflow_models import (
    WorkflowNode,
    WorkflowPlan,
    render_prompt_for_role,
)
from orchestrator_cli.core.workflow_validation import (
    collect_workflow_policy_diagnostics,
    validate_workflow_plan,
)
from orchestrator_cli.core.workflow_validation_workspace import (
    logical_workspace_selections,
)
from orchestrator_cli.version import SCHEMA_VERSION


def _write_workflow(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def _executor_prompt(node: WorkflowNode) -> str:
    return render_prompt_for_role(node, "executor")


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


class WorkflowCompositionImportTests(unittest.TestCase):
    def test_composes_imported_workflow_and_rewrites_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Auth Module",
                    "nodes:",
                    "  - id: plan",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## plan",
                    "",
                    "Build {{param:module_name}} for {{var:project_name}}.",
                ],
            )
            _write_workflow(
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
                    "  - id: summary.final",
                    "    mode: sequential",
                    "    needs: [auth.plan]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## summary.final",
                    "",
                    "Summarize {{auth.plan.output}}.",
                ],
            )

            load_result = load_tasks_with_sources(workflow, project_root=root)
            validated = validate_workflow_plan(load_result.workflow)

        self.assertEqual(
            [node.id for node in validated.nodes],
            ["auth.plan", "summary.final"],
        )
        self.assertIn("payments-auth", _executor_prompt(validated.nodes[0]))
        self.assertIn("{{var:project_name}}", _executor_prompt(validated.nodes[0]))
        self.assertEqual(
            [record.path.name for record in load_result.referenced_workflows],
            ["root.task.md", "module.task.md"],
        )

    def test_composes_imported_workflow_and_preserves_findings_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Review Module",
                    "nodes:",
                    "  - id: review",
                    "    mode: sequential",
                    "    findings: true",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## review",
                    "",
                    "Review the module.",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: module.task.md",
                    "    as: quality",
                    "nodes:",
                    "  - id: implement",
                    "    mode: sequential",
                    "    needs: [quality.review]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## implement",
                    "",
                    "Use {{quality.review.findings}}.",
                ],
            )

            validated = validate_workflow_plan(
                load_tasks_with_sources(workflow, project_root=root).workflow
            )

        self.assertEqual(
            [node.id for node in validated.nodes],
            ["quality.review", "implement"],
        )
        self.assertIn(
            "{{quality.review.findings}}", _executor_prompt(validated.nodes[1])
        )

    def test_unbound_param_template_rewrites_to_var_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
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
                    "Use {{param:module_name}}.",
                ],
            )
            _write_workflow(
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
                    "    needs: [auth.plan]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## summary",
                    "",
                    "done",
                ],
            )

            workflow_plan = load_tasks_with_sources(
                workflow,
                project_root=root,
            ).workflow

        self.assertIn("{{var:module_name}}", _executor_prompt(workflow_plan.nodes[0]))

    def test_imported_nodes_inherit_root_single_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
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
            _write_workflow(
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

        self.assertEqual(workflow_plan.nodes[0].id, "module.plan")
        self.assertIsNone(workflow_plan.nodes[0].worktree)
        selections = logical_workspace_selections(
            workflow_plan,
            _workspace_config(),
        )
        self.assertEqual(
            selections["module.plan"].logical_worktree_name,
            "primary",
        )

    def test_import_with_rejects_unused_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
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
            _write_workflow(
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

            with self.assertRaisesRegex(ValueError, "unused parameter"):
                load_tasks_with_sources(workflow, project_root=root)

    def test_import_with_rejects_shadowed_unused_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            leaf = root / "leaf.task.md"
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
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
            _write_workflow(
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
            _write_workflow(
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

            with self.assertRaisesRegex(ValueError, "unused parameter"):
                load_tasks_with_sources(workflow, project_root=root)

    def test_import_cycle_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workflow_a = root / "a.task.md"
            workflow_b = root / "b.task.md"

            _write_workflow(
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
            _write_workflow(
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

            with self.assertRaisesRegex(ValueError, "cycle"):
                load_tasks_with_sources(workflow_a, project_root=root)

    def test_composition_rejects_node_id_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
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
            _write_workflow(
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

            with self.assertRaisesRegex(ValueError, "Node ID collision"):
                load_tasks_with_sources(workflow, project_root=root)

    def test_imported_schema_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
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
            _write_workflow(
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

            with self.assertRaisesRegex(
                ValueError, "Unsupported workflow schema version"
            ):
                load_tasks_with_sources(workflow, project_root=root)

    def test_nested_imports_compose_alias_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared.task.md"
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                shared,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Shared",
                    "nodes:",
                    "  - id: normalize",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## normalize",
                    "",
                    "Normalize",
                ],
            )
            _write_workflow(
                module,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Module",
                    "imports:",
                    "  - path: shared.task.md",
                    "    as: shared",
                    "nodes:",
                    "  - id: finalize",
                    "    mode: sequential",
                    "    needs: [shared.normalize]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## finalize",
                    "",
                    "Use {{shared.normalize.output}}",
                ],
            )
            _write_workflow(
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
                    "    needs: [auth.finalize]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## summary",
                    "",
                    "Summary",
                ],
            )

            workflow_plan = load_tasks_with_sources(
                workflow,
                project_root=root,
            ).workflow

        ids = [node.id for node in workflow_plan.nodes]
        self.assertEqual(ids, ["auth.shared.normalize", "auth.finalize", "summary"])
        self.assertEqual(workflow_plan.nodes[1].needs, ["auth.shared.normalize"])
        self.assertIn(
            "{{auth.shared.normalize.output}}",
            _executor_prompt(workflow_plan.nodes[1]),
        )

    def test_output_reference_requires_upstream_dependency_across_import_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
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
            _write_workflow(
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
            with self.assertRaisesRegex(ValueError, "not an upstream dependency"):
                validate_workflow_plan(workflow_plan)

    def test_root_node_can_feed_imported_namespace_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            review = root / "review.task.md"
            fixer = root / "fix.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
                review,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Review",
                    "nodes:",
                    "  - id: findings",
                    "    mode: sequential",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## findings",
                    "",
                    "Findings",
                ],
            )
            _write_workflow(
                fixer,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Fix",
                    "nodes:",
                    "  - id: implement",
                    "    mode: sequential",
                    "    needs: [review-input]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## implement",
                    "",
                    "Use {{review-input.output}}",
                ],
            )
            _write_workflow(
                workflow,
                [
                    "---",
                    f'schema_version: "{SCHEMA_VERSION}"',
                    "name: Root",
                    "imports:",
                    "  - path: review.task.md",
                    "    as: quality",
                    "  - path: fix.task.md",
                    "    as: fix",
                    "nodes:",
                    "  - id: fix.review-input",
                    "    mode: sequential",
                    "    needs: [quality.findings]",
                    "    providers: [alpha]",
                    "---",
                    "",
                    "## fix.review-input",
                    "",
                    "Pass through {{quality.findings.output}}",
                ],
            )

            validated = validate_workflow_plan(
                load_tasks_with_sources(workflow, project_root=root).workflow
            )

        self.assertEqual(
            [node.id for node in validated.nodes],
            ["quality.findings", "fix.implement", "fix.review-input"],
        )
        self.assertEqual(validated.nodes[1].needs, ["fix.review-input"])
        self.assertIn(
            "{{fix.review-input.output}}", _executor_prompt(validated.nodes[1])
        )

    def test_imported_worktree_selector_is_namespace_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
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
            _write_workflow(
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

        self.assertEqual(
            [node.id for node in validated.nodes], ["auth.implement", "auth.fix"]
        )
        self.assertEqual(list(validated.worktrees), ["auth.implementation"])
        self.assertEqual(validated.nodes[0].worktree, "auth.implementation")
        self.assertEqual(validated.nodes[1].worktree, "auth.implementation")
        self.assertEqual(validated.nodes[1].needs, ["auth.implement"])
        selections = logical_workspace_selections(validated, _workspace_config())
        self.assertEqual(
            selections["auth.fix"].logical_worktree_name,
            "auth.implementation",
        )
        self.assertEqual(selections["auth.fix"].source_node_id, "auth.implement")

    def test_imported_worktree_none_selector_stays_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
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
            _write_workflow(
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

        self.assertEqual(list(validated.worktrees), ["auth.implementation"])
        self.assertEqual(validated.nodes[0].id, "auth.inspect")
        self.assertEqual(validated.nodes[0].worktree, "none")
        selection = logical_workspace_selections(
            validated,
            _workspace_config(),
        )["auth.inspect"]
        self.assertFalse(selection.enabled)
        self.assertIsNone(selection.logical_worktree_name)

    def test_imported_implicit_worktree_selector_uses_namespaced_declaration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
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
            _write_workflow(
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

        self.assertEqual(list(validated.worktrees), ["auth.implementation"])
        self.assertIsNone(validated.nodes[0].worktree)
        self.assertIsNone(validated.nodes[1].worktree)
        selections = logical_workspace_selections(
            validated,
            _workspace_config(clean_start="tracked_only"),
        )
        self.assertEqual(
            selections["auth.implement"].logical_worktree_name,
            "auth.implementation",
        )
        self.assertEqual(selections["auth.implement"].clean_start, "tracked_only")
        self.assertEqual(selections["auth.fix"].source_node_id, "auth.implement")
        self.assertEqual(selections["auth.fix"].clean_start, "tracked_only")
        self.assertEqual(
            _workspace_policy_messages(validated, clean_start="tracked_only"),
            (),
        )

    def test_nested_import_inherits_nearest_parent_single_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            child = root / "child.task.md"
            parent = root / "parent.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
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
            _write_workflow(
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
            _write_workflow(
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

        self.assertEqual(list(validated.worktrees), ["auth.implementation"])
        self.assertEqual([node.id for node in validated.nodes], ["auth.child.inspect"])
        self.assertIsNone(validated.nodes[0].worktree)
        selection = logical_workspace_selections(validated, _workspace_config())[
            "auth.child.inspect"
        ]
        self.assertTrue(selection.enabled)
        self.assertEqual(selection.logical_worktree_name, "auth.implementation")
        self.assertEqual(_workspace_policy_messages(validated), ())

    def test_root_node_does_not_inherit_only_imported_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
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
            _write_workflow(
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
                    '    source: "{{file:.orchestrator/inputs/requirements.md}}"',
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

        self.assertEqual(
            [node.id for node in validated.nodes],
            ["auth.implement", "requirements", "inspect"],
        )
        self.assertIsNone(validated.nodes[0].worktree)
        self.assertIsNone(validated.nodes[1].worktree)
        self.assertEqual(validated.nodes[2].worktree, "none")
        selections = logical_workspace_selections(validated, _workspace_config())
        self.assertEqual(
            selections["auth.implement"].logical_worktree_name,
            "auth.implementation",
        )
        self.assertFalse(selections["inspect"].enabled)
        self.assertEqual(selections["inspect"].materialization, "project_root")
        self.assertEqual(_workspace_policy_messages(validated), ())

    def test_imported_modules_keep_local_single_worktree_inheritance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            auth_module = root / "auth.task.md"
            billing_module = root / "billing.task.md"
            workflow = root / "root.task.md"

            for path, node_id in (
                (auth_module, "implement-auth"),
                (billing_module, "implement-billing"),
            ):
                _write_workflow(
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
            _write_workflow(
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

        self.assertEqual(
            list(validated.worktrees),
            ["auth.implementation", "billing.implementation"],
        )
        self.assertEqual(
            [node.worktree for node in validated.nodes],
            ["auth.implementation", "billing.implementation"],
        )
        self.assertEqual(_workspace_policy_messages(validated), ())

    def test_root_node_keeps_local_single_worktree_inheritance_with_imports(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module = root / "module.task.md"
            workflow = root / "root.task.md"

            _write_workflow(
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
            _write_workflow(
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

        self.assertEqual(
            list(validated.worktrees),
            ["auth.implementation", "scratch"],
        )
        self.assertEqual(validated.nodes[0].worktree, "auth.implementation")
        self.assertEqual(validated.nodes[1].worktree, "scratch")
        self.assertEqual(_workspace_policy_messages(validated), ())

    def test_root_worktree_declaration_remains_workflow_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workflow = root / "root.task.md"

            _write_workflow(
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

        self.assertEqual(list(loaded.worktrees), ["scratch"])
        self.assertIsNone(loaded.nodes[0].worktree)
        selections = logical_workspace_selections(loaded, _workspace_config())
        self.assertEqual(selections["inspect"].logical_worktree_name, "scratch")
        self.assertEqual(selections["inspect"].declaration_kind, "snapshot")
